from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.adapters.xray import (
    DEFAULT_XRAY_ADAPTER,
    XRAY_PUBLIC_HOST,
    XRAY_PUBLIC_PATH,
    XRAY_PUBLIC_PORT,
    XrayAdapterError,
    XrayApplyResult,
    XrayClient,
    XRAY_TRANSPORT,
)
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_json
from fwrouter_api.services.subscription_profiles import (
    list_desired_subscription_xray_clients,
    render_subscription_profile,
)
from fwrouter_api.services.xray_handoff import build_xray_handoff_assignments
from fwrouter_api.services.xray_subscription import build_xray_vless_uri
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.subject_inventory import sync_subject_inventory
import fwrouter_api.services.subject_policy as subject_policy_service
from fwrouter_api.services.subject_policy import get_subject_with_effective_state
from fwrouter_api.services.subjects import get_subject
from fwrouter_api.services.custom_servers import (
    VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
)


def _client_alias_map() -> dict[str, str | None]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT sx.client_id, sx.client_uuid, s.alias
            FROM subject_xray AS sx
            JOIN subjects AS s ON s.subject_id = sx.subject_id
            """
        ).fetchall()

    aliases: dict[str, str | None] = {}
    for row in rows:
        if row["client_id"]:
            aliases[str(row["client_id"])] = row["alias"]
        if row["client_uuid"]:
            aliases[str(row["client_uuid"])] = row["alias"]
    return aliases


def _subscription_path(client_id: str) -> str:
    return f"/api/v2/xray/clients/{client_id}/subscription"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            SELECT module_name, desired_state, runtime_state, apply_state,
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


def _xray_client_create_preflight(*, allow_blocked_egress: bool) -> dict[str, Any]:
    status = get_xray_status()
    module = status.get("module") if isinstance(status.get("module"), dict) else {}
    egress = status.get("egress") if isinstance(status.get("egress"), dict) else {}

    if str(module.get("desired_state") or "disabled") != "enabled":
        return {
            "ok": False,
            "code": "XRAY_MODULE_DISABLED",
            "message": "Xray module is disabled. Enable the module before creating client subscriptions.",
            "module": module,
            "egress": egress,
        }

    if bool(egress.get("traffic_available")) or allow_blocked_egress:
        return {
            "ok": True,
            "code": None,
            "message": "Xray client creation preflight passed.",
            "module": module,
            "egress": egress,
        }

    candidate = _xray_materializable_egress_candidate()
    if candidate["ok"]:
        return {
            "ok": True,
            "code": None,
            "message": "Xray egress is not active yet, but it can be materialized after client creation.",
            "module": module,
            "egress": egress,
            "materializable_egress": candidate,
        }

    return {
        "ok": False,
        "code": "XRAY_EGRESS_NOT_READY",
        "message": "Xray egress is not ready and no supported selected VPN server can be materialized.",
        "module": module,
        "egress": egress,
        "materializable_egress": candidate,
    }


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


def _safe_binding_for_state(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in binding.items()
        if key not in {"server_config"}
    }


def _bindings_for_state(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_safe_binding_for_state(binding) for binding in bindings]


def _annotate_bindings_with_handoff(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    handoff_by_server = {
        str(assignment["selected_server_id"]): assignment
        for assignment in build_xray_handoff_assignments(bindings)
    }
    annotated: list[dict[str, Any]] = []
    for binding in bindings:
        updated = dict(binding)
        selected_server_id = str(binding.get("selected_server_id") or "").strip()
        handoff = handoff_by_server.get(selected_server_id)
        if handoff is not None:
            updated["handoff"] = {
                "listener_name": handoff["listener_name"],
                "listen": handoff["listen"],
                "port": handoff["port"],
                "outbound_tag": handoff["tag"],
            }
        annotated.append(updated)
    return annotated


def collect_xray_runtime_bindings() -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT sx.subject_id
            FROM subject_xray AS sx
            JOIN subjects AS s ON s.subject_id = sx.subject_id
            WHERE sx.enabled = 1
              AND s.is_active = 1
              AND s.is_deleted = 0
            ORDER BY sx.subject_id
            """
        ).fetchall()

    routing = subject_policy_service.get_routing_snapshot()
    runtime_enforcement = subject_policy_service.build_runtime_enforcement_state()
    bypass_state = subject_policy_service.get_core_bypass_state()
    bindings: list[dict[str, Any]] = []
    for row in rows:
        subject = get_subject(str(row["subject_id"]))
        if not isinstance(subject, dict):
            continue
        subject = subject_policy_service.enrich_subject_with_effective_state(
            subject,
            routing=routing,
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        binding = _build_binding_for_subject(subject)
        if binding is not None:
            bindings.append(binding)

    return _annotate_bindings_with_handoff(bindings)


def get_xray_handoff_listeners(bindings: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    active_bindings = bindings if bindings is not None else collect_xray_runtime_bindings()
    return build_xray_handoff_assignments(active_bindings)


def _strip_raw_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_raw_payload(item)
            for key, item in value.items()
            if key != "raw"
        }
    if isinstance(value, list):
        return [_strip_raw_payload(item) for item in value]
    return value


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


def _xray_subject_for_client(client_id: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT subject_id
            FROM subject_xray
            WHERE client_id = ? OR client_uuid = ?
            LIMIT 1
            """,
            (client_id, client_id),
        ).fetchone()
    if row is None:
        return None
    return get_subject_with_effective_state(str(row["subject_id"]))


def _build_binding_for_subject(subject: dict[str, Any]) -> dict[str, Any] | None:
    detail = subject.get("detail") if isinstance(subject.get("detail"), dict) else {}
    effective_state = subject.get("effective_state") if isinstance(subject.get("effective_state"), dict) else {}
    scoped_runtime = (
        effective_state.get("scoped_runtime")
        if isinstance(effective_state.get("scoped_runtime"), dict)
        else {}
    )
    client_uuid = str(detail.get("client_uuid") or "").strip()
    client_id = str(detail.get("client_id") or "").strip()
    selected_server_id = effective_state.get("selected_server_id")
    selected_server_source = effective_state.get("selected_server_source")
    if not client_uuid and not client_id:
        return None
    if str(effective_state.get("dataplane_path") or "") != "vpn":
        return None

    # For Xray clients in vpn-auto mode, the actual target is the Mihomo selector,
    # not a concrete active_auto_server_id value from the DB.
    target_server_id_for_handoff = selected_server_id
    target_server_name = None
    target_server_raw_config = None

    if selected_server_source == "vpn_auto" or str(selected_server_id) == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
        target_server_id_for_handoff = "vpn-global"
        target_server_name = VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME
        server_config = None
    else:
        if selected_server_id is None:
            return None
        server_config = _load_server_config_for_xray_binding(
            str(selected_server_id) if selected_server_id is not None else None,
        )
        if server_config is None:
            return None
        target_server_name = server_config.get("server_name")
        target_server_raw_config = server_config.get("raw")

    return {
        "subject_id": subject.get("subject_id"),
        "client_id": client_id or client_uuid,
        "client_uuid": client_uuid or client_id,
        "client_email": detail.get("email"),
        "selected_server_id": target_server_id_for_handoff,
        "selected_server_source": effective_state.get("selected_server_source"),
        "handoff_proxy_name": (
            target_server_id_for_handoff
            if target_server_id_for_handoff == "vpn-global"
            else str(target_server_name or target_server_id_for_handoff)
        ),
        "server_name": target_server_name,
        "server_config": target_server_raw_config,
        "match_key": scoped_runtime.get("match_key"),
        "status": "pending",
        "applied_at": _utc_timestamp(),
    }


def _write_xray_bindings_state(bindings: list[dict[str, Any]], *, applied_ok: bool = False) -> dict[str, Any]:
    safe_bindings = _bindings_for_state(bindings)
    if applied_ok:
        for b in safe_bindings:
            b["status"] = "applied"

    handoff_listeners = get_xray_handoff_listeners(bindings)
    payload = {
        "bindings_version": 1,
        "generated_at": _utc_timestamp(),
        "bindings_count": len(safe_bindings),
        "applied_count": len([binding for binding in safe_bindings if binding.get("status") == "applied"]),
        "bindings": safe_bindings,
        "handoff_count": len(handoff_listeners),
        "handoff_listeners": handoff_listeners,
    }
    atomic_write_json(_xray_bindings_path(), payload)
    return payload


def _serialize_client(client: XrayClient, *, alias_override: str | None = None) -> dict[str, Any]:
    alias = alias_override if alias_override is not None else client.alias
    return {
        "client_id": client.client_id,
        "client_uuid": client.client_uuid,
        "email": client.email,
        "alias": alias,
        "enabled": client.enabled,
        "subscription_path": _subscription_path(client.client_id),
        "raw": client.raw,
    }


def _set_local_alias(client_id: str, alias: str | None) -> None:
    normalized_alias = alias.strip() if isinstance(alias, str) else None
    if normalized_alias == "":
        normalized_alias = None

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT subject_id
            FROM subject_xray
            WHERE client_id = ? OR client_uuid = ?
            LIMIT 1
            """,
            (client_id, client_id),
        ).fetchone()

        if row is None:
            return

        connection.execute(
            """
            UPDATE subjects
            SET alias = ?, updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (normalized_alias, row["subject_id"]),
        )


def _reload_failed_result(result: XrayApplyResult) -> bool:
    return bool(result.error_code) and result.error_code.startswith("XRAY_RELOAD")


def _sync_xray_inventory(requested_by: str) -> dict[str, Any]:
    return sync_subject_inventory(
        requested_by=requested_by,
        discover_docker=False,
        discover_tailscale=False,
        discover_xray=True,
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

    # Verify that required Mihomo egress ports are actually listening via socket probe
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


def list_xray_clients() -> list[dict[str, Any]]:
    aliases = _client_alias_map()
    return [
        _serialize_client(client, alias_override=aliases.get(client.client_id) or aliases.get(client.client_uuid))
        for client in DEFAULT_XRAY_ADAPTER.list_clients()
    ]


def create_xray_client(
    *,
    alias: str | None = None,
    email: str | None = None,
    requested_by: str = "api",
    allow_blocked_egress: bool = False,
) -> dict[str, Any]:
    preflight = _xray_client_create_preflight(allow_blocked_egress=allow_blocked_egress)
    if not preflight["ok"]:
        payload = {
            "ok": False,
            "status": "blocked",
            "stage": "preflight",
            "client": None,
            "subscription_uri": None,
            "preflight": preflight,
            "result": {
                "message": preflight["message"],
                "error_code": preflight["code"],
                "details": preflight,
            },
        }
        write_operational_log(
            event_type="xray_client_create_blocked",
            level="warning",
            message=preflight["message"],
            details={**payload, "requested_by": requested_by},
        )
        return payload

    result = DEFAULT_XRAY_ADAPTER.create_client(alias=alias, email=email)
    client_payload = dict(result.details.get("client") or {})
    client_id = str(client_payload.get("client_id") or "")

    if not result.ok:
        subscription = {"ok": False, "subscription_uri": None}
    else:
        if client_id:
            _sync_xray_inventory(requested_by)
            if alias is not None:
                _set_local_alias(client_id, alias)
            materialize_xray_runtime_bindings(requested_by=requested_by)

        subscription = (
            export_xray_subscription(client_id)
            if result.details.get("client") and client_id
            else {"ok": False, "subscription_uri": None}
        )

    payload = {
        "ok": result.ok,
        "status": "success" if result.ok else "failed",
        "stage": str(result.details.get("stage") or ("completed" if result.ok else "reload")),
        "client": (
            _serialize_client(
                XrayClient(
                    client_id=client_payload.get("client_id", client_id),
                    client_uuid=client_payload.get("client_uuid", client_id),
                    email=client_payload.get("email"),
                    alias=alias,
                    enabled=bool(client_payload.get("enabled", True)),
                    raw=dict(client_payload.get("raw") or {}),
                ),
                alias_override=alias,
            )
            if client_payload
            else None
        ),
        "subscription_uri": subscription.get("subscription_uri"),
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": _strip_raw_payload(result.details),
        },
    }

    if isinstance(payload.get("client"), dict):
        payload["client"].pop("raw", None)

    write_operational_log(
        event_type="xray_client_created" if result.ok else "xray_client_create_failed",
        level="info" if result.ok else "warning",
        subject_id=f"xray:{client_id}" if client_id else None,
        message=result.message,
        details=_strip_raw_payload(payload),
    )
    return payload


def delete_xray_client(client_id: str, *, requested_by: str = "api") -> dict[str, Any]:
    result = DEFAULT_XRAY_ADAPTER.delete_client(client_id)
    _sync_xray_inventory(requested_by)
    materialize_xray_runtime_bindings(requested_by=requested_by)

    payload = {
        "ok": result.ok,
        "status": "success" if result.ok else "failed",
        "stage": str(result.details.get("stage") or ("completed" if result.ok else "reload")),
        "client_id": client_id,
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": _strip_raw_payload(result.details),
        },
    }
    write_operational_log(
        event_type="xray_client_deleted" if result.ok else "xray_client_delete_failed",
        level="info" if result.ok else "warning",
        subject_id=f"xray:{client_id}",
        message=result.message,
        details=payload,
    )
    return payload


def update_xray_client_alias(
    client_id: str,
    *,
    alias: str | None,
    requested_by: str = "api",
) -> dict[str, Any]:
    result = DEFAULT_XRAY_ADAPTER.update_client_alias(client_id, alias)
    _set_local_alias(client_id, alias)

    payload = {
        "ok": result.ok,
        "status": "success" if result.ok else "failed",
        "client": result.details.get("client"),
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": _strip_raw_payload(result.details),
        },
    }
    write_operational_log(
        event_type="xray_client_alias_updated" if result.ok else "xray_client_alias_update_failed",
        level="info" if result.ok else "warning",
        subject_id=f"xray:{client_id}",
        message=result.message,
        details={**payload, "requested_by": requested_by},
    )
    return payload


def reload_xray(*, requested_by: str = "api") -> dict[str, Any]:
    materialized = materialize_xray_runtime_bindings(requested_by=requested_by)
    if not materialized["ok"]:
        return materialized
    payload = {
        "ok": True,
        "status": "success",
        "result": materialized["result"],
        "bindings_state": materialized["bindings_state"],
    }
    write_operational_log(
        event_type="xray_reloaded",
        level="info",
        message=str(materialized["result"]["message"]),
        details={**payload, "requested_by": requested_by},
    )
    return payload


def sync_xray_subjects(*, requested_by: str = "api") -> dict[str, Any]:
    result = _sync_xray_inventory(requested_by)
    if result["ok"]:
        materialized = materialize_xray_runtime_bindings(requested_by=requested_by)
    else:
        materialized = None
    payload = {
        "ok": result["ok"],
        "status": "success" if result["ok"] else "failed",
        "sync": result,
    }
    if materialized is not None:
        payload["materialize"] = materialized
    write_operational_log(
        event_type="xray_subjects_synced" if result["ok"] else "xray_subjects_sync_failed",
        level="info" if result["ok"] else "warning",
        message="Xray subject inventory synced." if result["ok"] else "Xray subject inventory sync failed.",
        details={**payload, "requested_by": requested_by},
    )
    write_technical_log(
        component="xray",
        event_type="xray_subjects_synced" if result["ok"] else "xray_subjects_sync_failed",
        level="info" if result["ok"] else "warning",
        message="Xray subject sync completed." if result["ok"] else "Xray subject sync failed.",
        details={**payload, "requested_by": requested_by},
    )
    return payload


def materialize_xray_runtime_bindings(
    *,
    requested_by: str = "api",
    prepare_mihomo_handoff: bool = True,
) -> dict[str, Any]:
    bindings = collect_xray_runtime_bindings()

    mihomo_handoff_prepare: dict[str, Any] | None = None
    if prepare_mihomo_handoff:
        from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime

        mihomo_handoff_prepare = reconcile_mihomo_runtime()
        if not mihomo_handoff_prepare.get("ok"):
            payload = {
                "ok": False,
                "status": "failed",
                "stage": "mihomo_handoff_prepare",
                "bindings_count": len(bindings),
                "mihomo_handoff_prepare": mihomo_handoff_prepare,
            }
            write_technical_log(
                component="xray",
                event_type="xray_binding_materialization_failed",
                level="warning",
                message="Failed to prepare Mihomo Xray handoff listeners.",
                details=payload,
            )
            write_operational_log(
                event_type="xray_binding_materialization_failed",
                level="warning",
                message="Failed to prepare Mihomo handoff for Xray bindings.",
                details={**payload, "requested_by": requested_by},
            )
            return payload

    result = DEFAULT_XRAY_ADAPTER.materialize_client_bindings(bindings)
    if not result.ok:
        payload = {
            "ok": False,
            "status": "failed",
            "error": {
                "code": result.error_code or "XRAY_BINDINGS_APPLY_FAILED",
                "message": result.message,
            },
            "bindings_count": len(bindings),
            "result": {
                "message": result.message,
                "error_code": result.error_code,
                "details": _strip_raw_payload(result.details),
            },
            "mihomo_handoff_prepare": mihomo_handoff_prepare,
        }
        write_technical_log(
            component="xray",
            event_type="xray_binding_materialization_failed",
            level="warning",
            message=result.message,
            details=payload,
        )
        write_operational_log(
            event_type="xray_binding_materialization_failed",
            level="warning",
            message=result.message,
            details={**payload, "requested_by": requested_by},
        )
        # Even on failure, we write the state but with 'pending' status
        _write_xray_bindings_state(bindings, applied_ok=False)
        return payload

    state = _write_xray_bindings_state(bindings, applied_ok=result.ok)
    payload = {
        "ok": True,
        "status": "success",
        "bindings_count": len(bindings),
        "bindings_state": state,
        "mihomo_handoff_prepare": mihomo_handoff_prepare,
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": _strip_raw_payload(result.details),
        },
    }
    write_operational_log(
        event_type="xray_binding_materialized",
        level="info",
        message="Xray runtime binding metadata materialized.",
        details={**payload, "requested_by": requested_by},
    )
    write_technical_log(
        component="xray",
        event_type="xray_binding_materialized",
        level="info",
        message="Xray runtime binding metadata materialized.",
        details={**payload, "requested_by": requested_by},
    )
    return payload


def _full_xray_client_uri(client: XrayClient, *, display_name: str | None = None) -> str:
    label = display_name or client.alias or client.email or client.client_id
    return build_xray_vless_uri(
        client_uuid=client.client_uuid,
        label=label,
    )


def _vpn_auto_xray_client_email(server_id: str) -> str:
    digest = hashlib.sha1(server_id.encode("utf-8")).hexdigest()[:12]
    return f"vpn-auto-{digest}@fwrouter.local"


def _is_subscription_profile_email(email: str) -> bool:
    return str(email or "").startswith("sub-")


def _vpn_auto_servers_for_xray_subscription() -> list[dict[str, Any]]:
    with db_session() as connection:
        vpn_auto_rows = connection.execute(
            """
            SELECT s.server_id, s.server_name, s.raw_json, ps.status AS ping_status, ps.last_ping_ms
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            LEFT JOIN server_ping_state AS ps ON ps.server_id = s.server_id
            WHERE COALESCE(p.vpn_auto, 0) = 1
              AND s.inventory_state = 'active'
              AND COALESCE(p.manually_deleted_at, '') = ''
              AND s.server_id NOT IN (
                  SELECT server_id FROM server_custom_https_proxy
              )
            ORDER BY
              CASE WHEN ps.status = 'success' THEN 0 ELSE 1 END,
              ps.last_ping_ms,
              s.server_id
            """
        ).fetchall()
        proxy_rows = connection.execute(
            """
            SELECT s.server_id, s.server_name, s.raw_json, ps.status AS ping_status, ps.last_ping_ms
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            LEFT JOIN server_ping_state AS ps ON ps.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.vpn_auto, 0) = 1
              AND COALESCE(p.manually_deleted_at, '') = ''
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()

    normal_servers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in vpn_auto_rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        supported, reason = _is_xray_supported_server_config(raw if isinstance(raw, dict) else None)
        if not supported:
            continue
        normal_servers.append(
            {
                "server_id": row["server_id"],
                "server_name": row["server_name"],
                "raw": raw,
                "ping_status": row["ping_status"],
                "last_ping_ms": row["last_ping_ms"],
                "support_reason": reason,
            }
        )
        seen_ids.add(str(row["server_id"]))

    proxy_server: dict[str, Any] | None = None
    for row in proxy_rows:
        server_id = str(row["server_id"])
        if server_id in seen_ids:
            continue
        proxy_server = {
            "server_id": server_id,
            "server_name": "Proxy (не заходить)",
            "raw": {"kind": "custom_https_proxy"},
            "ping_status": row["ping_status"],
            "last_ping_ms": row["last_ping_ms"],
            "support_reason": "custom_https_proxy",
        }
        seen_ids.add(server_id)

    result: list[dict[str, Any]] = [
        {
            "server_id": VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
            "server_name": VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
            "raw": {"kind": "xray_vpn_auto"},
            "ping_status": "virtual",
            "last_ping_ms": None,
            "support_reason": "virtual_xray_vpn_auto",
        }
    ]
    if proxy_server is not None:
        result.append(proxy_server)
    result.extend(normal_servers)
    return result


def _upsert_xray_subject_server_override(
    *,
    subject_id: str,
    selected_server_id: str,
    requested_by: str,
) -> None:
    selected_until = "2099-12-31 23:59:59"

    with db_session() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(subject_server_overrides)").fetchall()
        }

        subject_exists = connection.execute(
            "SELECT 1 FROM subjects WHERE subject_id = ? AND is_deleted = 0 LIMIT 1",
            (subject_id,),
        ).fetchone()
        if subject_exists is None:
            return

        server_exists = connection.execute(
            "SELECT 1 FROM servers WHERE server_id = ? LIMIT 1",
            (selected_server_id,),
        ).fetchone()
        if server_exists is None:
            return

        existing = connection.execute(
            "SELECT 1 FROM subject_server_overrides WHERE subject_id = ? LIMIT 1",
            (subject_id,),
        ).fetchone()

        if existing is None:
            insert_values: dict[str, Any] = {}
            if "subject_id" in columns:
                insert_values["subject_id"] = subject_id
            if "selected_server_id" in columns:
                insert_values["selected_server_id"] = selected_server_id
            if "selected_until" in columns:
                insert_values["selected_until"] = selected_until
            if "requested_by" in columns:
                insert_values["requested_by"] = requested_by
            if "created_by" in columns:
                insert_values["created_by"] = requested_by
            if "updated_by" in columns:
                insert_values["updated_by"] = requested_by

            literal_columns: list[str] = []
            literal_values: list[str] = []
            if "created_at" in columns:
                literal_columns.append("created_at")
                literal_values.append("CURRENT_TIMESTAMP")
            if "updated_at" in columns:
                literal_columns.append("updated_at")
                literal_values.append("CURRENT_TIMESTAMP")

            names = list(insert_values.keys()) + literal_columns
            placeholders = ["?"] * len(insert_values) + literal_values

            connection.execute(
                f"""
                INSERT INTO subject_server_overrides ({", ".join(names)})
                VALUES ({", ".join(placeholders)})
                """,
                tuple(insert_values.values()),
            )
        else:
            assignments: list[str] = []
            params: list[Any] = []

            if "selected_server_id" in columns:
                assignments.append("selected_server_id = ?")
                params.append(selected_server_id)
            if "selected_until" in columns:
                assignments.append("selected_until = ?")
                params.append(selected_until)
            if "requested_by" in columns:
                assignments.append("requested_by = ?")
                params.append(requested_by)
            if "updated_by" in columns:
                assignments.append("updated_by = ?")
                params.append(requested_by)
            if "updated_at" in columns:
                assignments.append("updated_at = CURRENT_TIMESTAMP")

            params.append(subject_id)
            connection.execute(
                f"""
                UPDATE subject_server_overrides
                SET {", ".join(assignments)}
                WHERE subject_id = ?
                """,
                tuple(params),
            )


def reconcile_xray_vpn_auto_subscription(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    module = _module_state("xray") or {}
    if str(module.get("desired_state") or "") != "enabled":
        return {
            "ok": True,
            "status": "skipped",
            "reason": "xray_module_disabled",
            "created_count": 0,
            "deleted_count": 0,
            "nodes_count": 0,
        }

    servers = _vpn_auto_servers_for_xray_subscription()
    desired_by_email: dict[str, dict[str, Any]] = {
        _vpn_auto_xray_client_email(str(server["server_id"])): server
        for server in servers
    }

    existing_clients = {
        str(client.email or ""): client
        for client in DEFAULT_XRAY_ADAPTER.list_clients()
        if str(client.email or "")
    }

    created: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []

    for email, client in list(existing_clients.items()):
        if not email.startswith("vpn-auto-"):
            continue
        if email in desired_by_email:
            continue

        result = DEFAULT_XRAY_ADAPTER.delete_client(client.client_id or client.client_uuid)
        if not result.ok:
            return {
                "ok": False,
                "status": "failed",
                "stage": "delete_stale_client",
                "error_code": result.error_code or "XRAY_VPN_AUTO_STALE_DELETE_FAILED",
                "error_message": result.message,
                "client_id": client.client_id,
                "email": email,
                "details": _strip_raw_payload(result.details),
            }

        deleted.append(
            {
                "client_id": client.client_id,
                "client_uuid": client.client_uuid,
                "email": email,
            }
        )
        existing_clients.pop(email, None)

    nodes: list[dict[str, Any]] = []

    for email, server in desired_by_email.items():
        server_id = str(server["server_id"])
        server_name = str(server["server_name"] or server_id)

        client = existing_clients.get(email)
        if client is None:
            result = DEFAULT_XRAY_ADAPTER.create_client(alias=server_name, email=email)
            if not result.ok:
                return {
                    "ok": False,
                    "status": "failed",
                    "stage": "create_client",
                    "error_code": result.error_code or "XRAY_VPN_AUTO_CLIENT_CREATE_FAILED",
                    "error_message": result.message,
                    "server_id": server_id,
                    "email": email,
                    "details": _strip_raw_payload(result.details),
                }

            client_payload = dict((result.details or {}).get("client") or {})
            client = XrayClient(
                client_id=str(client_payload.get("client_id") or client_payload.get("client_uuid") or ""),
                client_uuid=str(client_payload.get("client_uuid") or client_payload.get("client_id") or ""),
                email=email,
                alias=server_name,
                enabled=True,
                raw=dict(client_payload.get("raw") or {}),
            )
            existing_clients[email] = client
            created.append(
                {
                    "client_id": client.client_id,
                    "client_uuid": client.client_uuid,
                    "email": email,
                    "server_id": server_id,
                    "server_name": server_name,
                }
            )

        nodes.append(
            {
                "server_id": server_id,
                "server_name": server_name,
                "email": email,
                "client": client,
            }
        )

    _sync_xray_inventory(requested_by)

    for node in nodes:
        client = node["client"]
        server_id = str(node["server_id"])
        server_name = str(node["server_name"])

        _set_local_alias(client.client_id or client.client_uuid, server_name)

        subject = _xray_subject_for_client(client.client_uuid) or _xray_subject_for_client(client.client_id)
        if subject is None:
            return {
                "ok": False,
                "status": "failed",
                "stage": "subject_lookup",
                "error_code": "XRAY_VPN_AUTO_SUBJECT_MISSING",
                "error_message": f"Xray subject was not created for client {client.client_uuid or client.client_id}.",
                "server_id": server_id,
                "email": node["email"],
            }

        _upsert_xray_subject_server_override(
            subject_id=str(subject["subject_id"]),
            selected_server_id=server_id,
            requested_by=requested_by,
        )

    profile_reconcile = reconcile_xray_subscription_profile_nodes(
        requested_by=requested_by,
        materialize=False,
    )
    if not profile_reconcile.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "subscription_profiles",
            "error_code": profile_reconcile.get("error_code") or "XRAY_SUBSCRIPTION_PROFILE_RECONCILE_FAILED",
            "error_message": profile_reconcile.get("error_message") or "Failed to reconcile subscription profile nodes.",
            "profile_reconcile": profile_reconcile,
            "created": created,
            "deleted": deleted,
        }

    from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime

    mihomo_reconcile = reconcile_mihomo_runtime()
    if not mihomo_reconcile.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "mihomo_handoff_prepare",
            "error_code": "XRAY_VPN_AUTO_MIHOMO_RECONCILE_FAILED",
            "error_message": "Failed to prepare Mihomo Xray handoff listeners.",
            "mihomo_reconcile": mihomo_reconcile,
            "created": created,
            "deleted": deleted,
        }

    materialize = materialize_xray_runtime_bindings(
        requested_by=requested_by,
        prepare_mihomo_handoff=False,
    )
    if not materialize.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "materialize",
            "error_code": "XRAY_VPN_AUTO_MATERIALIZE_FAILED",
            "error_message": "Failed to materialize Xray vpn-auto bindings.",
            "mihomo_reconcile": mihomo_reconcile,
            "materialize": materialize,
            "created": created,
            "deleted": deleted,
        }

    return {
        "ok": True,
        "status": "success",
        "created_count": len(created),
        "deleted_count": len(deleted),
        "nodes_count": len(nodes),
        "created": created,
        "deleted": deleted,
        "profile_reconcile": profile_reconcile,
        "mihomo_reconcile": mihomo_reconcile,
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "email": node["email"],
                "client_id": node["client"].client_id,
                "client_uuid": node["client"].client_uuid,
            }
            for node in nodes
        ],
        "materialize": materialize,
    }


def reconcile_xray_subscription_profile_nodes(
    *,
    requested_by: str = "api",
    materialize: bool = True,
    token_or_slug: str | None = None, # <-- Added this parameter back
) -> dict[str, Any]:
    module = _module_state("xray") or {}
    if str(module.get("desired_state") or "") != "enabled":
        return {
            "ok": True,
            "status": "skipped",
            "reason": "xray_module_disabled",
            "nodes_count": 0,
        }

    desired_nodes = list_desired_subscription_xray_clients(token_or_slug)
    desired_by_email = {
        str(node["client_email"]): node
        for node in desired_nodes
        if str(node.get("client_email") or "").strip()
    }
    existing_clients = {
        str(client.email or ""): client
        for client in DEFAULT_XRAY_ADAPTER.list_clients()
        if str(client.email or "")
    }

    created: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    recreated: list[dict[str, Any]] = []

    for email, client in list(existing_clients.items()):
        if not _is_subscription_profile_email(email):
            continue
        if email in desired_by_email:
            continue
        result = DEFAULT_XRAY_ADAPTER.delete_client(client.client_id or client.client_uuid)
        if not result.ok:
            return {
                "ok": False,
                "status": "failed",
                "stage": "delete_stale_profile_client",
                "error_code": result.error_code or "XRAY_SUB_PROFILE_DELETE_FAILED",
                "error_message": result.message,
                "email": email,
                "details": _strip_raw_payload(result.details),
            }
        deleted.append(
            {
                "client_id": client.client_id,
                "client_uuid": client.client_uuid,
                "email": email,
            }
        )
        existing_clients.pop(email, None)

    for email, node in desired_by_email.items():
        existing = existing_clients.get(email)
        desired_uuid = str(node["client_uuid"])
        alias = str(node["xray_alias"])
        if existing is not None and str(existing.client_uuid) != desired_uuid:
            result = DEFAULT_XRAY_ADAPTER.delete_client(existing.client_id or existing.client_uuid)
            if not result.ok:
                return {
                    "ok": False,
                    "status": "failed",
                    "stage": "replace_profile_client_delete",
                    "error_code": result.error_code or "XRAY_SUB_PROFILE_REPLACE_DELETE_FAILED",
                    "error_message": result.message,
                    "email": email,
                    "details": _strip_raw_payload(result.details),
                }
            recreated.append(
                {
                    "email": email,
                    "old_client_uuid": existing.client_uuid,
                    "new_client_uuid": desired_uuid,
                }
            )
            existing_clients.pop(email, None)
            existing = None

        if existing is None:
            result = DEFAULT_XRAY_ADAPTER.create_client(
                alias=alias,
                email=email,
                client_uuid=desired_uuid,
            )
            if not result.ok:
                return {
                    "ok": False,
                    "status": "failed",
                    "stage": "create_profile_client",
                    "error_code": result.error_code or "XRAY_SUB_PROFILE_CREATE_FAILED",
                    "error_message": result.message,
                    "email": email,
                    "details": _strip_raw_payload(result.details),
                }
            client_payload = dict((result.details or {}).get("client") or {})
            existing = XrayClient(
                client_id=str(client_payload.get("client_id") or desired_uuid),
                client_uuid=str(client_payload.get("client_uuid") or desired_uuid),
                email=email,
                alias=alias,
                enabled=True,
                raw=dict(client_payload.get("raw") or {}),
            )
            existing_clients[email] = existing
            created.append(
                {
                    "client_id": existing.client_id,
                    "client_uuid": existing.client_uuid,
                    "email": email,
                    "server_id": node["server_id"],
                }
            )

    _sync_xray_inventory(requested_by)

    for node in desired_nodes:
        client_uuid = str(node["client_uuid"])
        subject = _xray_subject_for_client(client_uuid)
        if subject is None:
            return {
                "ok": False,
                "status": "failed",
                "stage": "profile_subject_lookup",
                "error_code": "XRAY_SUB_PROFILE_SUBJECT_MISSING",
                "error_message": f"Xray subject was not created for profile client {client_uuid}.",
                "client_uuid": client_uuid,
                "email": node["client_email"],
            }
        _set_local_alias(client_uuid, str(node["xray_alias"]))
        _upsert_xray_subject_server_override(
            subject_id=str(subject["subject_id"]),
            selected_server_id=str(node["server_id"]),
            requested_by=requested_by,
        )

    materialize_result: dict[str, Any] | None = None
    if materialize:
        materialize_result = materialize_xray_runtime_bindings(requested_by=requested_by)
        if not materialize_result.get("ok"):
            return {
                "ok": False,
                "status": "failed",
                "stage": "materialize",
                "error_code": "XRAY_SUB_PROFILE_MATERIALIZE_FAILED",
                "error_message": "Failed to materialize Xray subscription profile bindings.",
                "materialize": materialize_result,
            }

    return {
        "ok": True,
        "status": "success",
        "nodes_count": len(desired_nodes),
        "created_count": len(created),
        "deleted_count": len(deleted),
        "recreated_count": len(recreated),
        "created": created,
        "deleted": deleted,
        "recreated": recreated,
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "client_uuid": node["client_uuid"],
                "client_email": node["client_email"],
            }
            for node in desired_nodes
        ],
        "materialize": materialize_result,
    }


def export_subscription_profile_text(
    token_or_slug: str,
    *,
    user_agent: str | None,
    requested_format: str | None,
) -> dict[str, Any]:
    return render_subscription_profile(
        token_or_slug,
        user_agent=user_agent,
        requested_format=requested_format,
    )


def export_xray_vpn_auto_subscription_text(
    *,
    base64_encode: bool = True,
    requested_by: str = "api",
) -> dict[str, Any]:
    servers = _vpn_auto_servers_for_xray_subscription()
    if not servers:
        return {
            "ok": False,
            "content": "",
            "uris": [],
            "nodes_count": 0,
            "error_code": "XRAY_VPN_AUTO_EMPTY",
            "error_message": "No supported vpn-auto servers are available for Xray subscription.",
        }

    existing_clients = {
        str(client.email or ""): client
        for client in DEFAULT_XRAY_ADAPTER.list_clients()
        if str(client.email or "")
    }

    nodes: list[dict[str, Any]] = []
    created_any = False

    for server in servers:
        server_id = str(server["server_id"])
        server_name = str(server["server_name"] or server_id)
        email = _vpn_auto_xray_client_email(server_id)

        client = existing_clients.get(email)
        if client is None:
            result = DEFAULT_XRAY_ADAPTER.create_client(alias=server_name, email=email)
            if not result.ok:
                return {
                    "ok": False,
                    "content": "",
                    "uris": [],
                    "nodes_count": len(nodes),
                    "error_code": result.error_code or "XRAY_VPN_AUTO_CLIENT_CREATE_FAILED",
                    "error_message": result.message,
                    "details": _strip_raw_payload(result.details),
                }

            client_payload = dict((result.details or {}).get("client") or {})
            client = XrayClient(
                client_id=str(client_payload.get("client_id") or client_payload.get("client_uuid") or ""),
                client_uuid=str(client_payload.get("client_uuid") or client_payload.get("client_id") or ""),
                email=email,
                alias=server_name,
                enabled=True,
                raw=dict(client_payload.get("raw") or {}),
            )
            existing_clients[email] = client
            created_any = True

        nodes.append(
            {
                "server_id": server_id,
                "server_name": server_name,
                "client": client,
                "uri": _full_xray_client_uri(client, display_name=server_name),
            }
        )

    # Normal subscription refresh must be fast and must not restart Xray.
    # Heavy reconciliation is only needed when missing node-clients had to be created.
    if created_any:
        _sync_xray_inventory(requested_by)

        for node in nodes:
            client = node["client"]
            server_id = str(node["server_id"])
            server_name = str(node["server_name"])

            _set_local_alias(client.client_id, server_name)

            subject = _xray_subject_for_client(client.client_uuid) or _xray_subject_for_client(client.client_id)
            if subject is None:
                return {
                    "ok": False,
                    "content": "",
                    "uris": [],
                    "nodes_count": len(nodes),
                    "error_code": "XRAY_VPN_AUTO_SUBJECT_MISSING",
                    "error_message": f"Xray subject was not created for client {client.client_uuid}.",
                }

            _upsert_xray_subject_server_override(
                subject_id=str(subject["subject_id"]),
                selected_server_id=server_id,
                requested_by=requested_by,
            )

        materialize = materialize_xray_runtime_bindings(requested_by=requested_by)
        if not materialize.get("ok"):
            return {
                "ok": False,
                "content": "",
                "uris": [node["uri"] for node in nodes],
                "nodes_count": len(nodes),
                "error_code": "XRAY_VPN_AUTO_MATERIALIZE_FAILED",
                "error_message": "Failed to materialize vpn-auto Xray subscription bindings.",
                "materialize": materialize,
            }
    else:
        materialize = {
            "ok": True,
            "status": "skipped",
            "reason": "subscription_read_only_refresh",
        }

    raw_content = chr(10).join(node["uri"] for node in nodes) + chr(10)
    content = (
        base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
        if base64_encode
        else raw_content
    )

    return {
        "ok": True,
        "content": content,
        "uris": [node["uri"] for node in nodes],
        "base64": base64_encode,
        "nodes_count": len(nodes),
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "client_id": node["client"].client_id,
                "client_uuid": node["client"].client_uuid,
                "email": node["client"].email,
            }
            for node in nodes
        ],
        "materialize": materialize,
    }

def export_xray_subscription_text(
    client_id: str,
    *,
    base64_encode: bool = True,
) -> dict[str, Any]:
    aliases = _client_alias_map()

    target: XrayClient | None = None
    for client in DEFAULT_XRAY_ADAPTER.list_clients():
        if client.client_id == client_id or client.client_uuid == client_id:
            target = client
            break

    if target is None:
        return {
            "ok": False,
            "content": "",
            "uris": [],
            "error_code": "XRAY_CLIENT_NOT_FOUND",
            "error_message": f"Xray client not found: {client_id}",
        }

    uri = _full_xray_client_uri(
        target,
        display_name=aliases.get(target.client_id) or aliases.get(target.client_uuid),
    )
    raw_content = uri + "\n"
    content = (
        base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
        if base64_encode
        else raw_content
    )

    return {
        "ok": True,
        "content": content,
        "uris": [uri],
        "base64": base64_encode,
        "nodes_count": 1,
        "client_id": target.client_id,
        "client_uuid": target.client_uuid,
    }


def export_xray_subscription(client_id: str) -> dict[str, Any]:
    result = DEFAULT_XRAY_ADAPTER.export_vless_subscription(client_id)
    details = dict(result.details)
    subject = _xray_subject_for_client(client_id)
    effective_state = subject.get("effective_state") if isinstance(subject, dict) and isinstance(subject.get("effective_state"), dict) else {}
    scoped_runtime = effective_state.get("scoped_runtime") if isinstance(effective_state.get("scoped_runtime"), dict) else None
    return {
        "ok": result.ok,
        "client_id": client_id,
        "public_host": XRAY_PUBLIC_HOST,
        "public_port": XRAY_PUBLIC_PORT,
        "public_path": XRAY_PUBLIC_PATH,
        "transport": "ws",
        "security": "tls",
        "subscription_uri": details.get("subscription_uri"),
        "subject_id": subject.get("subject_id") if isinstance(subject, dict) else None,
        "server_binding": {
            "selected_server_id": effective_state.get("selected_server_id"),
            "selected_server_source": effective_state.get("selected_server_source"),
            "effective_mode": effective_state.get("effective_mode"),
            "dataplane_path": effective_state.get("dataplane_path"),
            "scoped_runtime": scoped_runtime,
            "binding_saved": bool(effective_state.get("selected_server_id")),
            "binding_applied": bool(scoped_runtime and scoped_runtime.get("status") == "applied"),
            "binding_verified": bool(scoped_runtime and scoped_runtime.get("status") == "applied"),
        },
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": details,
        },
    }


def xray_service_call(fn: Any, *args: Any, **kwargs: Any) -> tuple[bool, dict[str, Any]]:
    try:
        return True, fn(*args, **kwargs)
    except XrayAdapterError as exc:
        payload = {
            "ok": False,
            "status": "failed",
            "error": {
                "code": exc.code,
                "message": exc.message,
            },
            "details": exc.details,
        }
        write_technical_log(
            component="xray",
            event_type="xray_service_error",
            level="warning",
            message=exc.message,
            details=payload,
        )
        return False, payload
