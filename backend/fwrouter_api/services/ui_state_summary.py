from __future__ import annotations

from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.logs import list_operational_logs, list_technical_logs
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.subjects import get_subject
from fwrouter_api.services.subscription import get_subscription_state
from fwrouter_api.services.traffic import get_traffic_accounting_state
from fwrouter_api.services.xray import get_xray_status
from fwrouter_api.services.ui_display_settings import _display_systems
from fwrouter_api.services.ui_state_clients import _ui_workspace_counts
from fwrouter_api.services.ui_state_common import _active_job, _job_summary, _system_subject_counts
from fwrouter_api.services.ui_state_inventory import list_ui_settings_inventory
from fwrouter_api.services.ui_state_logs import _summarize_log_event
from fwrouter_api.services.ui_state_settings import get_ui_display_settings


def get_router_self_subject() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT subject_id
            FROM subject_fwrouter
            WHERE component_name = 'global'
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return get_subject(str(row["subject_id"]))


def _server_name_by_id(server_id: str | None) -> str | None:
    normalized = str(server_id or "").strip()
    if not normalized:
        return None
    with db_session() as connection:
        row = connection.execute(
            """
            SELECT server_name
            FROM servers
            WHERE server_id = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
    return str(row["server_name"]).strip() if row and row["server_name"] else None


def get_ui_router_summary() -> dict[str, Any]:
    return get_live_probe_cache(
        "ui_state.router_summary",
        ttl_seconds=2.0,
        loader=_build_ui_router_summary,
    )


def _build_ui_router_summary() -> dict[str, Any]:
    routing = get_routing_global_state() or {}
    router_subject = get_router_self_subject()
    active_apply_job = _active_job("apply")
    fixed_server_id = str(
        routing.get("applied_fixed_server_id")
        or routing.get("desired_fixed_server_id")
        or ""
    ).strip()
    current_server_name = (
        _server_name_by_id(fixed_server_id)
        if fixed_server_id
        else str(routing.get("active_auto_server_id") or "").strip() or None
    )
    return {
        "global_mode": str(routing.get("applied_mode") or routing.get("desired_mode") or "direct").upper(),
        "global_mode_desired": str(routing.get("desired_mode") or "direct").upper(),
        "selective_default": str(routing.get("selective_default") or "direct").upper(),
        "router_self_mode": str((router_subject or {}).get("applied_mode") or (router_subject or {}).get("desired_mode") or "disabled").upper(),
        "router_self_mode_desired": str((router_subject or {}).get("desired_mode") or "disabled").upper(),
        "router_self_subject_id": (router_subject or {}).get("subject_id"),
        "router_self_display_name": (router_subject or {}).get("display_name"),
        "server_mode": str(routing.get("server_mode") or "auto").upper(),
        "active_auto_server_id": routing.get("active_auto_server_id"),
        "fixed_server_id": fixed_server_id or None,
        "current_server_name": current_server_name,
        "current_server_source": "manual" if fixed_server_id else "vpn-auto",
        "routing_apply_state": routing.get("apply_state"),
        "routing_error_code": routing.get("error_code"),
        "routing_error_message": routing.get("error_message"),
        "active_job": _job_summary(active_apply_job),
    }


def get_ui_settings_workspace() -> dict[str, Any]:
    return get_live_probe_cache(
        "ui_state.settings_workspace",
        ttl_seconds=2.0,
        loader=_build_ui_settings_workspace,
    )


def _build_ui_settings_workspace() -> dict[str, Any]:
    display_settings = get_ui_display_settings()
    counts = _ui_workspace_counts(display_settings=display_settings)
    modules = fetch_modules()
    subscription = dict(get_subscription_state() or {})
    subscription["url_saved"] = bool(subscription.get("url"))
    xray = get_xray_status()
    counts.update(_system_subject_counts())
    operational_logs = [
        _summarize_log_event(item)
        for item in list_operational_logs(limit=20)
    ]
    technical_logs = [
        _summarize_log_event(item, technical=True)
        for item in list_technical_logs(limit=20)
    ]
    return {
        "display_settings": display_settings,
        "display_systems": _display_systems(display_settings=display_settings, counts=counts, modules=modules),
        "modules": modules,
        "router": get_ui_router_summary(),
        "subscription": subscription,
        "traffic": get_traffic_accounting_state(),
        "xray": xray,
        "counts": counts,
        "logs": {
            "operational_recent": operational_logs,
            "technical_recent": technical_logs,
            "operational_count": len(operational_logs),
            "technical_count": len(technical_logs),
        },
    }
