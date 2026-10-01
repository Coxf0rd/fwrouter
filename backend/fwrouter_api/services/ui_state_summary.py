from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.events import safe_human_label
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.logs import list_operational_logs, list_technical_logs
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.subjects import get_subject
from fwrouter_api.services.subscription import (
    compact_subscription_metadata,
    get_subscription_state,
    redact_subscription_public_value,
)
from fwrouter_api.services.traffic import get_traffic_accounting_state
from fwrouter_api.services.xray import get_xray_status
from fwrouter_api.services.ui_display_settings import _display_systems
from fwrouter_api.services.ui_state_clients import _ui_workspace_counts
from fwrouter_api.services.ui_state_common import _active_job, _job_summary, _system_subject_counts
from fwrouter_api.services.ui_state_inventory import list_ui_settings_inventory
from fwrouter_api.services.ui_state_logs import _summarize_log_event, summarize_ui_log_events
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


def _current_auto_provenance(active_auto_server_id: Any, *, server_mode: str) -> dict[str, Any] | None:
    if server_mode != "auto" or not str(active_auto_server_id or "").strip():
        return None
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = 'routing.auto_selection_provenance'"
        ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or str(value.get("selected_server_id") or "") != str(active_auto_server_id):
        return None
    reason = str(value.get("reason_code") or "")
    origin = str(value.get("origin") or "")
    if reason not in {"api_controlled_switch", "automatic_selection", "watchdog_failover", "watchdog_initial_select", "initial_selection", "manual", "subscription_refresh_auto_select", "vpn_auto_membership_changed", "server_preferences_vpn_auto", "scheduler_watchdog_check"}:
        return None
    if origin not in {"api", "ui", "watchdog", "subscription", "server_preferences", "unknown"}:
        return None
    return {
        "reason_code": reason,
        "origin": origin,
        "actor_attribution": "caller_supplied_unverified",
        "selected_at": value.get("selected_at"),
        "server_label": safe_human_label(
            value.get("selected_server_label"), entity_id=active_auto_server_id
        ),
    }


def get_ui_router_summary() -> dict[str, Any]:
    return get_live_probe_cache(
        "ui_state.router_summary",
        ttl_seconds=2.0,
        loader=_build_ui_router_summary,
    )


def _build_ui_router_summary() -> dict[str, Any]:
    routing = get_routing_global_state(expire_ttl=False) or {}
    router_subject = get_router_self_subject()
    active_apply_job = _active_job("apply")
    server_mode = str(routing.get("server_mode") or "auto").strip().lower()
    fixed_server_id = ""
    if server_mode == "fixed":
        fixed_server_id = str(
            routing.get("applied_fixed_server_id")
            or routing.get("desired_fixed_server_id")
            or ""
        ).strip()
    current_server_name = (
        _server_name_by_id(fixed_server_id)
        if fixed_server_id
        else _server_name_by_id(str(routing.get("active_auto_server_id") or "").strip())
    )
    active_auto_server_id = routing.get("active_auto_server_id")
    return {
        "global_mode": str(routing.get("applied_mode") or routing.get("desired_mode") or "direct").upper(),
        "global_mode_desired": str(routing.get("desired_mode") or "direct").upper(),
        "selective_default": str(routing.get("selective_default") or "direct").upper(),
        "router_self_mode": str((router_subject or {}).get("applied_mode") or (router_subject or {}).get("desired_mode") or "disabled").upper(),
        "router_self_mode_desired": str((router_subject or {}).get("desired_mode") or "disabled").upper(),
        "router_self_subject_id": (router_subject or {}).get("subject_id"),
        "router_self_display_name": (router_subject or {}).get("display_name"),
        "server_mode": server_mode.upper(),
        "active_auto_server_id": active_auto_server_id,
        "current_server_auto_provenance": _current_auto_provenance(active_auto_server_id, server_mode=server_mode),
        "fixed_server_id": fixed_server_id or None,
        "current_server_id": fixed_server_id or str(routing.get("active_auto_server_id") or "").strip() or None,
        "current_server_name": current_server_name,
        "current_server_source": "manual" if fixed_server_id else "auto",
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
    subscription_url = subscription.pop("url", None)
    subscription["url_saved"] = bool(subscription_url)
    subscription["metadata"] = compact_subscription_metadata(subscription.get("metadata"), redact_urls=True)
    subscription = redact_subscription_public_value(subscription)
    from fwrouter_api.services.provider_managed import provider_projection
    subscription["provider_managed"] = provider_projection()
    xray = get_xray_status()
    counts.update(_system_subject_counts())
    operational_logs = summarize_ui_log_events(list_operational_logs(limit=20))
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
