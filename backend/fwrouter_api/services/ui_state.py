from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.jobs import get_active_lock_lease, get_job_without_cleanup
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache, get_live_probe_cache
from fwrouter_api.services.logs import list_operational_logs, list_technical_logs
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.runtime_prewarm import prime_runtime_read_models_async
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.subjects import get_subject
from fwrouter_api.services.subject_taxonomy import external_network_source_display_contract
from fwrouter_api.services.subscription import get_subscription_state
from fwrouter_api.services.subject_groups import XRAY_SUBSCRIPTION_GROUP_PREFIX, xray_subscription_group_from_row
from fwrouter_api.services.system_subjects import list_system_subjects
from fwrouter_api.services.traffic import get_traffic_accounting_state
from fwrouter_api.services.xray import get_xray_status
from fwrouter_api.services.ui_state_logs import _summarize_log_event
from fwrouter_api.services.ui_display_settings import (
    UI_DISPLAY_SETTINGS_KEY,
    UI_SYSTEM_VISIBILITY_DEFAULTS,
    _display_systems,
    _normalize_custom_external_systems,
    _normalize_system_visibility,
    _system_visible,
)


XRAY_INTERNAL_PREFIXES = ("sub-", "vpn-auto-")
XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS = 24 * 60 * 60
TRAFFIC_METRIC_KEYS = (
    "direct_rx_bytes",
    "direct_tx_bytes",
    "vpn_rx_bytes",
    "vpn_tx_bytes",
)
DEFAULT_TRAFFIC_PANEL_KEYS = ["vpn_rx_bytes", "vpn_tx_bytes"]
TRAFFIC_METRIC_LABELS = {
    "direct_rx_bytes": "DIRECT вход",
    "direct_tx_bytes": "DIRECT выход",
    "vpn_rx_bytes": "VPN вход",
    "vpn_tx_bytes": "VPN выход",
}
INVENTORY_ROLE_BY_KIND = {
    "lan": "lan_client",
    "tailscale": "external_network_source",
    "tailscale_node": "external_network_source",
    "xray": "vless_client",
    "docker": "docker_runtime",
    "host": "host_runtime",
    "fwrouter": "router_core",
}
INVENTORY_ROLE_ALIASES = {
    "lan": "lan_client",
    "lan_client": "lan_client",
    "external_network": "external_network_source",
    "external_network_source": "external_network_source",
    "vless": "vless_client",
    "client_core": "vless_client",
    "vless_client": "vless_client",
    "docker": "docker_runtime",
    "docker_runtime": "docker_runtime",
    "host": "host_runtime",
    "host_runtime": "host_runtime",
    "router": "router_core",
    "router_core": "router_core",
}
KINDS_BY_INVENTORY_ROLE = {
    "lan_client": {"lan"},
    "external_network_source": {"tailscale", "tailscale_node"},
    "vless_client": {"xray"},
    "docker_runtime": {"docker"},
    "host_runtime": {"host"},
    "router_core": {"fwrouter"},
}


def _default_display_settings() -> dict[str, Any]:
    return {
        "system_visibility": dict(UI_SYSTEM_VISIBILITY_DEFAULTS),
        "custom_external_systems": [],
        "show_inactive": False,
        "show_internal_vless": False,
        "hidden_subject_ids": [],
        "subject_traffic_preferences": {},
    }


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _inventory_role_for_kind(kind: Any) -> str:
    return INVENTORY_ROLE_BY_KIND.get(str(kind or "").strip().lower(), "unknown")


def _display_system_id_for_external_network_source(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    contract = external_network_source_display_contract(normalized)
    if contract:
        return str(contract["system_id"])
    slug = "".join(character if character.isalnum() else "-" for character in normalized).strip("-")
    return f"external-network-{slug}" if slug else "external_network_source"


def _normalize_inventory_role(role: Any) -> str:
    normalized = str(role or "all").strip().lower()
    if normalized in {"", "all"}:
        return "all"
    return INVENTORY_ROLE_ALIASES.get(normalized, normalized)


def _load_setting(key: str) -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return _json_loads(row["value_json"]) if row else None


def _save_setting(key: str, value: dict[str, Any]) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (key, _json_dumps(value)),
        )


def get_ui_display_settings() -> dict[str, Any]:
    state = _default_display_settings()
    saved = _load_setting(UI_DISPLAY_SETTINGS_KEY)
    if isinstance(saved, dict):
        state["custom_external_systems"] = _normalize_custom_external_systems(saved.get("custom_external_systems"))
        state["system_visibility"] = _normalize_system_visibility(
            saved,
            {
                str(system.get("system_id") or "")
                for system in state["custom_external_systems"]
            },
        )
        for key in ("show_inactive", "show_internal_vless"):
            if key in saved:
                state[key] = bool(saved.get(key))
        hidden_subject_ids = saved.get("hidden_subject_ids")
        if isinstance(hidden_subject_ids, list):
            state["hidden_subject_ids"] = [
                str(item).strip()
                for item in hidden_subject_ids
                if str(item).strip()
            ]
        traffic_preferences = saved.get("subject_traffic_preferences")
        if isinstance(traffic_preferences, dict):
            normalized_preferences: dict[str, list[str]] = {}
            for subject_id, metrics in traffic_preferences.items():
                normalized = _normalize_traffic_metric_keys(metrics)
                if normalized:
                    normalized_preferences[str(subject_id).strip()] = normalized
            state["subject_traffic_preferences"] = normalized_preferences
    return state


def save_ui_display_settings(payload: dict[str, Any]) -> dict[str, Any]:
    state = _default_display_settings()
    state["custom_external_systems"] = _normalize_custom_external_systems(payload.get("custom_external_systems"))
    state["system_visibility"] = _normalize_system_visibility(
        payload,
        {
            str(system.get("system_id") or "")
            for system in state["custom_external_systems"]
        },
    )
    for custom_system in state["custom_external_systems"]:
        system_id = custom_system["system_id"]
        state["system_visibility"].setdefault(system_id, True)
    for key in ("show_inactive", "show_internal_vless"):
        if key in payload:
            state[key] = bool(payload.get(key))
    hidden_subject_ids = payload.get("hidden_subject_ids")
    if isinstance(hidden_subject_ids, list):
        state["hidden_subject_ids"] = [
            str(item).strip()
            for item in hidden_subject_ids
            if str(item).strip()
        ]
    traffic_preferences = payload.get("subject_traffic_preferences")
    if isinstance(traffic_preferences, dict):
        normalized_preferences: dict[str, list[str]] = {}
        for subject_id, metrics in traffic_preferences.items():
            normalized = _normalize_traffic_metric_keys(metrics)
            if normalized:
                normalized_preferences[str(subject_id).strip()] = normalized
        state["subject_traffic_preferences"] = normalized_preferences
    _save_setting(UI_DISPLAY_SETTINGS_KEY, state)
    clear_live_probe_cache()
    prime_runtime_read_models_async(include_global_profiles=False)
    return state


def _month_key(timestamp: datetime | None = None) -> str:
    current = timestamp or datetime.now(timezone.utc)
    return current.strftime("%Y-%m")


def _parse_ui_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _subscription_group_token(group_subject_id: str) -> str:
    normalized = str(group_subject_id or "").strip().lower()
    if not normalized.startswith(XRAY_SUBSCRIPTION_GROUP_PREFIX):
        return ""
    return normalized[len(XRAY_SUBSCRIPTION_GROUP_PREFIX):].strip()


def _subscription_client_recent(subscription_client: dict[str, Any]) -> bool:
    if not subscription_client or not bool(subscription_client.get("enabled")):
        return False
    seen_at = _parse_ui_timestamp(subscription_client.get("last_seen_at"))
    if seen_at is None:
        return False
    age = datetime.now(timezone.utc) - seen_at
    return timedelta(seconds=0) <= age <= timedelta(seconds=XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS)


def _activity_state(
    *,
    is_active: bool,
    last_seen_at: Any = None,
    last_traffic_at: Any = None,
    subscription_recent: bool = False,
    subscription_group: bool = False,
) -> dict[str, str]:
    if subscription_group and subscription_recent:
        return {
            "activity_reason": "profile_seen_24h",
            "activity_reason_label": "Профиль запрашивался за 24ч",
        }
    if is_active and last_traffic_at:
        return {
            "activity_reason": "traffic_seen",
            "activity_reason_label": "Был трафик",
        }
    if is_active:
        return {
            "activity_reason": "runtime_active",
            "activity_reason_label": "Runtime активен",
        }
    if last_seen_at or last_traffic_at:
        return {
            "activity_reason": "stale_seen",
            "activity_reason_label": "Нет свежей активности",
        }
    return {
        "activity_reason": "unknown",
        "activity_reason_label": "Нет данных активности",
    }


def _normalize_traffic_metric_keys(value: Any) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_TRAFFIC_PANEL_KEYS)
    normalized: list[str] = []
    for item in value:
        key = str(item or "").strip()
        if key not in TRAFFIC_METRIC_KEYS:
            continue
        if key in normalized:
            continue
        normalized.append(key)
        if len(normalized) >= 2:
            break
    if len(normalized) == 2:
        return normalized
    return list(DEFAULT_TRAFFIC_PANEL_KEYS)


def _subject_traffic_metric_keys(subject_id: str, display_settings: dict[str, Any]) -> list[str]:
    preferences = display_settings.get("subject_traffic_preferences")
    if isinstance(preferences, dict):
        keys = preferences.get(subject_id)
        return _normalize_traffic_metric_keys(keys)
    return list(DEFAULT_TRAFFIC_PANEL_KEYS)


def _panel_traffic_metrics(subject_id: str, month_breakdown: dict[str, int], display_settings: dict[str, Any]) -> list[dict[str, Any]]:
    metric_keys = _subject_traffic_metric_keys(subject_id, display_settings)
    return [
        {
            "key": key,
            "label": TRAFFIC_METRIC_LABELS[key],
            "bytes": int(month_breakdown.get(key, 0)),
        }
        for key in metric_keys
    ]


def _traffic_maps() -> tuple[dict[str, int], dict[str, int], dict[str, dict[str, int]]]:
    return get_live_probe_cache(
        "ui_state.traffic_maps",
        ttl_seconds=5.0,
        loader=_load_traffic_maps,
    )


def _load_traffic_maps() -> tuple[dict[str, int], dict[str, int], dict[str, dict[str, int]]]:
    current_month = _month_key()
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                subject_id,
                SUM(COALESCE(direct_rx_bytes, 0)) AS direct_rx_total_bytes,
                SUM(COALESCE(direct_tx_bytes, 0)) AS direct_tx_total_bytes,
                SUM(COALESCE(vpn_rx_bytes, 0)) AS vpn_rx_total_bytes,
                SUM(COALESCE(vpn_tx_bytes, 0)) AS vpn_tx_total_bytes,
                SUM(
                    COALESCE(direct_rx_bytes, 0) +
                    COALESCE(direct_tx_bytes, 0) +
                    COALESCE(vpn_rx_bytes, 0) +
                    COALESCE(vpn_tx_bytes, 0) +
                    COALESCE(blocked_rx_bytes, 0) +
                    COALESCE(blocked_tx_bytes, 0)
                ) AS total_bytes,
                SUM(CASE WHEN period_month = ? THEN COALESCE(direct_rx_bytes, 0) ELSE 0 END) AS direct_rx_month_bytes,
                SUM(CASE WHEN period_month = ? THEN COALESCE(direct_tx_bytes, 0) ELSE 0 END) AS direct_tx_month_bytes,
                SUM(CASE WHEN period_month = ? THEN COALESCE(vpn_rx_bytes, 0) ELSE 0 END) AS vpn_rx_month_bytes,
                SUM(CASE WHEN period_month = ? THEN COALESCE(vpn_tx_bytes, 0) ELSE 0 END) AS vpn_tx_month_bytes,
                SUM(
                    CASE WHEN period_month = ? THEN
                        COALESCE(direct_rx_bytes, 0) +
                        COALESCE(direct_tx_bytes, 0) +
                        COALESCE(vpn_rx_bytes, 0) +
                        COALESCE(vpn_tx_bytes, 0) +
                        COALESCE(blocked_rx_bytes, 0) +
                        COALESCE(blocked_tx_bytes, 0)
                    ELSE 0 END
                ) AS current_month_bytes
            FROM traffic_monthly
            GROUP BY subject_id
            """,
            (current_month, current_month, current_month, current_month, current_month),
        ).fetchall()

    total_map: dict[str, int] = {}
    month_map: dict[str, int] = {}
    month_breakdown_map: dict[str, dict[str, int]] = {}
    for row in rows:
        subject_id = str(row["subject_id"])
        total_map[subject_id] = int(row["total_bytes"] or 0)
        month_map[subject_id] = int(row["current_month_bytes"] or 0)
        month_breakdown_map[subject_id] = {
            "direct_rx_bytes": int(row["direct_rx_month_bytes"] or 0),
            "direct_tx_bytes": int(row["direct_tx_month_bytes"] or 0),
            "vpn_rx_bytes": int(row["vpn_rx_month_bytes"] or 0),
            "vpn_tx_bytes": int(row["vpn_tx_month_bytes"] or 0),
            "direct_rx_total_bytes": int(row["direct_rx_total_bytes"] or 0),
            "direct_tx_total_bytes": int(row["direct_tx_total_bytes"] or 0),
            "vpn_rx_total_bytes": int(row["vpn_rx_total_bytes"] or 0),
            "vpn_tx_total_bytes": int(row["vpn_tx_total_bytes"] or 0),
        }
    return total_map, month_map, month_breakdown_map


def _subscription_client_map() -> dict[str, dict[str, Any]]:
    return get_live_probe_cache(
        "ui_state.subscription_clients",
        ttl_seconds=5.0,
        loader=_load_subscription_client_map,
    )


def _load_subscription_client_map() -> dict[str, dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT client_id, token, app_type, enabled, display_name, last_seen_at, last_user_agent
            FROM subscription_clients
            """
        ).fetchall()

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        token = str(row["token"] or "").strip().lower()
        if not token:
            continue
        result[token] = {
            "subscription_client_id": row["client_id"],
            "token": row["token"],
            "app_type": row["app_type"],
            "enabled": bool(row["enabled"]),
            "display_name": row["display_name"],
            "last_seen_at": row["last_seen_at"],
            "last_user_agent": row["last_user_agent"],
        }
    return result


def _list_effective_subjects_for_ui() -> list[dict[str, Any]]:
    return list_subjects_with_effective_state(include_deleted=False, limit=1000)


def _effective_state_by_subject_for_ui() -> dict[str, dict[str, Any]]:
    effective_subjects = get_live_probe_cache(
        "ui_state.effective_subjects",
        ttl_seconds=15.0,
        loader=_list_effective_subjects_for_ui,
    )
    return {
        str(item["subject_id"]): dict(item.get("effective_state") or {})
        for item in effective_subjects
    }


def _active_user_override_modes(subject_ids: list[str]) -> dict[str, str]:
    normalized = [str(subject_id).strip() for subject_id in subject_ids if str(subject_id).strip()]
    if not normalized:
        return {}
    placeholders = ", ".join("?" for _ in normalized)
    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT subject_id, override_mode
            FROM subject_user_overrides
            WHERE subject_id IN ({placeholders})
              AND override_mode IS NOT NULL
              AND override_until > CURRENT_TIMESTAMP
            """,
            tuple(normalized),
        ).fetchall()
    return {
        str(row["subject_id"]): str(row["override_mode"])
        for row in rows
        if str(row["override_mode"] or "").strip()
    }


def _human_xray_email(email: str) -> bool:
    normalized = str(email or "").strip().lower()
    return bool(normalized) and not normalized.startswith(XRAY_INTERNAL_PREFIXES)


def _xray_internal(email: str) -> bool:
    normalized = str(email or "").strip().lower()
    return normalized.startswith(XRAY_INTERNAL_PREFIXES)


def _xray_service_subject(email: str) -> bool:
    normalized = str(email or "").strip().lower()
    return normalized.startswith("vpn-auto-")


def _xray_legacy_subscription_shadow(email: str, subscription_map: dict[str, dict[str, Any]]) -> bool:
    normalized = str(email or "").strip().lower()
    if not normalized or normalized.startswith(XRAY_INTERNAL_PREFIXES):
        return False
    return _localpart(normalized) in subscription_map


def _localpart(email: str) -> str:
    return str(email or "").split("@", 1)[0].strip().lower()


def _xray_subscription_group(row: Any) -> tuple[str, str] | None:
    return xray_subscription_group_from_row(row)


def _sum_month_breakdowns(subject_ids: list[str], month_breakdown_map: dict[str, dict[str, int]]) -> dict[str, int]:
    result = {key: 0 for key in TRAFFIC_METRIC_KEYS}
    for subject_id in subject_ids:
        breakdown = month_breakdown_map.get(subject_id, {})
        for key in TRAFFIC_METRIC_KEYS:
            result[key] += int(breakdown.get(key, 0))
    return result


def _latest_text(values: list[Any]) -> Any:
    present = [value for value in values if value]
    return max(present) if present else None


def _xray_group_mode(values: list[Any], default: str = "enabled") -> str:
    present = [str(value or default).strip().lower() for value in values if str(value or "").strip()]
    if not present:
        return default.upper()
    if len(set(present)) == 1:
        return present[0].upper()
    if "enabled" in present:
        return "ENABLED"
    return present[0].upper()


def _xray_opaque_subscription_label(label: Any) -> bool:
    return str(label or "").strip().lower().startswith("sub-")


def _row_bool(row: Any, key: str) -> bool:
    return bool(int(row[key] or 0))


def _active_job(lock_key: str) -> dict[str, Any] | None:
    lease = get_active_lock_lease(lock_key)
    if lease is None:
        return None
    job_id = str(lease.get("owner_job_id") or "").strip()
    if not job_id:
        return None
    return get_job_without_cleanup(job_id)


def _job_summary(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    return {
        "job_id": job.get("job_id"),
        "job_type": job.get("job_type"),
        "status": job.get("status"),
        "requested_by": job.get("requested_by"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "error_code": job.get("error_code"),
        "error_message": job.get("error_message"),
    }


def _summarize_system_subject(subject: dict[str, Any]) -> dict[str, Any]:
    detail = subject.get("detail") if isinstance(subject.get("detail"), dict) else {}
    metadata = subject.get("metadata") if isinstance(subject.get("metadata"), dict) else {}
    return {
        "subject_id": subject.get("subject_id"),
        "subject_type": subject.get("subject_type"),
        "display_name": subject.get("display_name"),
        "alias": subject.get("alias"),
        "desired_mode": subject.get("desired_mode"),
        "applied_mode": subject.get("applied_mode"),
        "apply_state": subject.get("apply_state"),
        "runtime_state": subject.get("runtime_state"),
        "visibility": subject.get("visibility"),
        "is_active": bool(subject.get("is_active")),
        "is_deleted": bool(subject.get("is_deleted")),
        "can_delete": bool(subject.get("can_delete")),
        "last_seen_at": subject.get("last_seen_at"),
        "updated_at": subject.get("updated_at"),
        "detail": {
            "container_name": detail.get("container_name"),
            "project": detail.get("project"),
            "service": detail.get("service"),
            "host": detail.get("host"),
            "port": detail.get("port"),
            "protocol": detail.get("protocol"),
            "status": detail.get("status"),
        },
        "metadata": {
            "component_name": metadata.get("component_name"),
            "source": metadata.get("source"),
        },
    }


def _system_subject_counts() -> dict[str, int]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT subject_type, subject_role, COUNT(*) AS count
            FROM subjects
            WHERE is_deleted = 0
              AND subject_type IN ('docker', 'host', 'fwrouter')
            GROUP BY subject_type, subject_role
            """
        ).fetchall()

    counts = {
        "docker": 0,
        "host": 0,
        "fwrouter": 0,
        "docker_runtime": 0,
        "host_runtime": 0,
        "router_core": 0,
    }
    for row in rows:
        subject_type = str(row["subject_type"] or "")
        if subject_type in counts:
            value = int(row["count"] or 0)
            counts[subject_type] = value
            inventory_role = str(row["subject_role"] or _inventory_role_for_kind(subject_type))
            if inventory_role in counts:
                counts[inventory_role] = value
    return counts


def list_ui_clients() -> list[dict[str, Any]]:
    display_settings = get_ui_display_settings()
    total_map, month_map, month_breakdown_map = _traffic_maps()
    subscription_map = _subscription_client_map()
    effective_state_by_subject = _effective_state_by_subject_for_ui()

    with db_session() as connection:
        lan_rows = connection.execute(
            """
            SELECT
                s.subject_id,
                s.subject_type,
                s.subject_role,
                s.implementation_kind,
                s.display_name,
                s.alias,
                s.desired_mode,
                s.applied_mode,
                s.apply_state,
                s.runtime_state,
                s.is_active,
                s.last_seen_at,
                s.last_traffic_at,
                sl.ip_address,
                sl.mac_address,
                sl.hostname
            FROM subjects s
            JOIN subject_lan sl ON sl.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
            """
        ).fetchall()

        tailscale_rows = connection.execute(
            """
            SELECT
                s.subject_id,
                s.subject_type,
                s.subject_role,
                s.implementation_kind,
                s.display_name,
                s.alias,
                s.desired_mode,
                s.applied_mode,
                s.apply_state,
                s.runtime_state,
                s.is_active,
                s.last_seen_at,
                s.last_traffic_at,
                st.tailscale_ip,
                st.hostname,
                st.user_name,
                st.online
            FROM subjects s
            JOIN subject_tailscale st ON st.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
            """
        ).fetchall()

        xray_rows = connection.execute(
            """
            SELECT
                s.subject_id,
                s.subject_type,
                s.subject_role,
                s.implementation_kind,
                s.display_name,
                s.alias,
                s.desired_mode,
                s.applied_mode,
                s.apply_state,
                s.runtime_state,
                s.is_active,
                s.last_seen_at,
                s.last_traffic_at,
                sx.client_id,
                sx.client_uuid,
                sx.email,
                sx.subscription_path,
                sx.last_subscription_at,
                sx.enabled
            FROM subjects s
            JOIN subject_xray sx ON sx.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            ORDER BY COALESCE(s.last_seen_at, s.updated_at) DESC
            """
        ).fetchall()

    clients: list[dict[str, Any]] = []

    for row in lan_rows:
        subject_id = str(row["subject_id"])
        effective_state = effective_state_by_subject.get(subject_id, {})
        month_breakdown = month_breakdown_map.get(subject_id, {})
        clients.append(
            {
                "subject_id": subject_id,
                "kind": "lan",
                "inventory_role": str(row["subject_role"] or "lan_client"),
                "implementation_kind": str(row["implementation_kind"] or "lan"),
                "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["ip_address"] or subject_id),
                "alias": row["alias"],
                "hostname": row["hostname"],
                "ip_address": row["ip_address"],
                "mac_address": row["mac_address"],
                "mode_source": str(effective_state.get("mode_source") or "global").upper(),
                "effective_mode": str(effective_state.get("effective_mode") or row["desired_mode"] or "global").upper(),
                "committed_desired_mode": str(row["desired_mode"] or "global").upper(),
                "desired_mode": str(row["desired_mode"] or "global").upper(),
                "applied_mode": str(row["applied_mode"] or row["desired_mode"] or "global").upper(),
                "apply_state": str(row["apply_state"] or "clean"),
                "runtime_state": row["runtime_state"],
                "is_active": _row_bool(row, "is_active"),
                "is_internal": False,
                "last_seen_at": row["last_seen_at"],
                "last_traffic_at": row["last_traffic_at"],
                "traffic_total_bytes": int(total_map.get(subject_id, 0)),
                "traffic_month_bytes": int(month_map.get(subject_id, 0)),
                "traffic_month": month_breakdown,
                "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
            }
        )

    for row in tailscale_rows:
        subject_id = str(row["subject_id"])
        effective_state = effective_state_by_subject.get(subject_id, {})
        month_breakdown = month_breakdown_map.get(subject_id, {})
        clients.append(
            {
                "subject_id": subject_id,
                "kind": "tailscale",
                "inventory_role": str(row["subject_role"] or "external_network_source"),
                "implementation_kind": str(row["implementation_kind"] or row["subject_type"] or "tailscale_node"),
                "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["tailscale_ip"] or subject_id),
                "alias": row["alias"],
                "hostname": row["hostname"],
                "ip_address": row["tailscale_ip"],
                "mac_address": None,
                "user_name": row["user_name"],
                "online": _row_bool(row, "online"),
                "mode_source": str(effective_state.get("mode_source") or "global").upper(),
                "effective_mode": str(effective_state.get("effective_mode") or row["desired_mode"] or "global").upper(),
                "committed_desired_mode": str(row["desired_mode"] or "global").upper(),
                "desired_mode": str(row["desired_mode"] or "global").upper(),
                "applied_mode": str(row["applied_mode"] or row["desired_mode"] or "global").upper(),
                "apply_state": str(row["apply_state"] or "clean"),
                "runtime_state": row["runtime_state"],
                "is_active": _row_bool(row, "is_active"),
                "is_internal": False,
                "last_seen_at": row["last_seen_at"],
                "last_traffic_at": row["last_traffic_at"],
                "traffic_total_bytes": int(total_map.get(subject_id, 0)),
                "traffic_month_bytes": int(month_map.get(subject_id, 0)),
                "traffic_month": month_breakdown,
                "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
            }
        )

    grouped_xray: dict[str, dict[str, Any]] = {}

    for row in xray_rows:
        subject_id = str(row["subject_id"])
        effective_state = effective_state_by_subject.get(subject_id, {})
        email = str(row["email"] or "")
        if _xray_service_subject(email):
            continue
        if _xray_legacy_subscription_shadow(email, subscription_map):
            continue
        token = _localpart(email)
        subscription_client: dict[str, Any] = {}
        alias = str(row["alias"] or "").strip() or None
        display_name = (
            alias
            or str(subscription_client.get("display_name") or "").strip()
            or str(row["display_name"] or "").strip()
            or token
            or str(row["client_id"] or subject_id)
        )
        last_seen_at = subscription_client.get("last_seen_at") or row["last_seen_at"]
        month_breakdown = month_breakdown_map.get(subject_id, {})
        group = _xray_subscription_group(row)
        if group is not None:
            group_subject_id, group_label = group
            subscription_client = subscription_map.get(_subscription_group_token(group_subject_id), {})
            subscription_recent = _subscription_client_recent(subscription_client)
            last_seen_at = subscription_client.get("last_seen_at") or row["last_seen_at"]
            bucket = grouped_xray.setdefault(
                group_subject_id,
                {
                    "subject_id": group_subject_id,
                    "subject_ids": [],
                    "kind": "xray",
                    "inventory_role": str(row["subject_role"] or "vless_client"),
                    "implementation_kind": str(row["implementation_kind"] or "xray"),
                    "display_name": group_label,
                    "alias": group_label,
                    "email": email,
                    "client_id": None,
                    "client_uuid": None,
                    "subscription_path": None,
                    "subscription_client": subscription_client,
                    "mode_source_values": [],
                    "effective_mode_values": [],
                    "desired_mode_values": [],
                    "applied_mode_values": [],
                    "apply_state_values": [],
                    "runtime_state_values": [],
                    "is_active": False,
                    "is_internal": False,
                    "is_human": False,
                    "enabled": False,
                    "last_seen_values": [],
                    "last_traffic_values": [],
                    "traffic_total_bytes": 0,
                    "traffic_month_bytes": 0,
                    "traffic_month": {key: 0 for key in TRAFFIC_METRIC_KEYS},
                    "last_subscription_values": [],
                    "last_user_agent_values": [],
                    "member_count": 0,
                    "is_aggregate": True,
                    "aggregate_kind": "xray_subscription",
                    "can_delete": False,
                },
            )
            bucket["subject_ids"].append(subject_id)
            bucket["member_count"] += 1
            bucket["traffic_total_bytes"] += int(total_map.get(subject_id, 0))
            bucket["traffic_month_bytes"] += int(month_map.get(subject_id, 0))
            for key in TRAFFIC_METRIC_KEYS:
                bucket["traffic_month"][key] += int(month_breakdown.get(key, 0))
            bucket["mode_source_values"].append(effective_state.get("mode_source") or "enabled")
            bucket["effective_mode_values"].append(effective_state.get("effective_mode") or row["desired_mode"] or "enabled")
            bucket["desired_mode_values"].append(row["desired_mode"] or "enabled")
            bucket["applied_mode_values"].append(row["applied_mode"] or row["desired_mode"] or "enabled")
            bucket["apply_state_values"].append(row["apply_state"] or "clean")
            bucket["runtime_state_values"].append(row["runtime_state"])
            bucket["is_active"] = bool(bucket["is_active"]) or _row_bool(row, "is_active") or subscription_recent
            bucket["enabled"] = bool(bucket["enabled"]) or _row_bool(row, "enabled")
            if subscription_client and not bucket["subscription_client"]:
                bucket["subscription_client"] = subscription_client
            bucket["last_seen_values"].append(last_seen_at)
            bucket["last_traffic_values"].append(row["last_traffic_at"])
            bucket["last_subscription_values"].append(row["last_subscription_at"])
            bucket["last_user_agent_values"].append(subscription_client.get("last_user_agent"))
            continue

        clients.append(
            {
                "subject_id": subject_id,
                "kind": "xray",
                "inventory_role": str(row["subject_role"] or "vless_client"),
                "implementation_kind": str(row["implementation_kind"] or "xray"),
                "display_name": display_name,
                "alias": alias,
                "email": email,
                "client_id": row["client_id"],
                "client_uuid": row["client_uuid"],
                "subscription_path": row["subscription_path"],
                "subscription_client": subscription_client,
                "mode_source": str(effective_state.get("mode_source") or "enabled").upper(),
                "effective_mode": str(effective_state.get("effective_mode") or row["desired_mode"] or "enabled").upper(),
                "committed_desired_mode": str(row["desired_mode"] or "enabled").upper(),
                "desired_mode": str(row["desired_mode"] or "enabled").upper(),
                "applied_mode": str(row["applied_mode"] or row["desired_mode"] or "enabled").upper(),
                "apply_state": str(row["apply_state"] or "clean"),
                "runtime_state": row["runtime_state"],
                "is_active": _row_bool(row, "is_active") or bool(subscription_client.get("last_seen_at")),
                **_activity_state(
                    is_active=_row_bool(row, "is_active"),
                    last_seen_at=last_seen_at,
                    last_traffic_at=row["last_traffic_at"],
                ),
                "is_internal": _xray_internal(email),
                "is_human": _human_xray_email(email),
                "enabled": _row_bool(row, "enabled"),
                "last_seen_at": last_seen_at,
                "last_traffic_at": row["last_traffic_at"],
                "traffic_total_bytes": int(total_map.get(subject_id, 0)),
                "traffic_month_bytes": int(month_map.get(subject_id, 0)),
                "traffic_month": month_breakdown,
                "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
                "last_subscription_at": row["last_subscription_at"],
                "last_user_agent": subscription_client.get("last_user_agent"),
            }
        )

    for bucket in grouped_xray.values():
        if _xray_opaque_subscription_label(bucket["display_name"]):
            continue
        subject_id = str(bucket["subject_id"])
        month_breakdown = dict(bucket["traffic_month"])
        group_is_active = bool(bucket["is_active"])
        group_last_seen_at = _latest_text(bucket["last_seen_values"])
        group_last_traffic_at = _latest_text(bucket["last_traffic_values"])
        group_subscription_recent = _subscription_client_recent(bucket["subscription_client"])
        clients.append(
            {
                "subject_id": subject_id,
                "subject_ids": list(bucket["subject_ids"]),
                "kind": "xray",
                "inventory_role": str(bucket["inventory_role"] or "vless_client"),
                "implementation_kind": str(bucket["implementation_kind"] or "xray"),
                "display_name": bucket["display_name"],
                "alias": bucket["alias"],
                "email": bucket["email"],
                "client_id": bucket["client_id"],
                "client_uuid": bucket["client_uuid"],
                "subscription_path": bucket["subscription_path"],
                "subscription_client": bucket["subscription_client"],
                "mode_source": _xray_group_mode(bucket["mode_source_values"], "enabled"),
                "effective_mode": _xray_group_mode(bucket["effective_mode_values"], "enabled"),
                "committed_desired_mode": _xray_group_mode(bucket["desired_mode_values"], "enabled"),
                "desired_mode": _xray_group_mode(bucket["desired_mode_values"], "enabled"),
                "applied_mode": _xray_group_mode(bucket["applied_mode_values"], "enabled"),
                "apply_state": "failed" if "failed" in {str(item or "").lower() for item in bucket["apply_state_values"]} else "clean",
                "runtime_state": _latest_text(bucket["runtime_state_values"]),
                "is_active": group_is_active,
                **_activity_state(
                    is_active=group_is_active,
                    last_seen_at=group_last_seen_at,
                    last_traffic_at=group_last_traffic_at,
                    subscription_recent=group_subscription_recent,
                    subscription_group=True,
                ),
                "is_internal": False,
                "is_human": False,
                "enabled": bool(bucket["enabled"]),
                "last_seen_at": group_last_seen_at,
                "last_traffic_at": group_last_traffic_at,
                "traffic_total_bytes": int(bucket["traffic_total_bytes"]),
                "traffic_month_bytes": int(bucket["traffic_month_bytes"]),
                "traffic_month": month_breakdown,
                "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
                "last_subscription_at": _latest_text(bucket["last_subscription_values"]),
                "last_user_agent": _latest_text(bucket["last_user_agent_values"]),
                "member_count": int(bucket["member_count"]),
                "is_aggregate": True,
                "aggregate_kind": "xray_subscription",
                "can_delete": False,
            }
        )

    kind_rank = {"lan": 0, "tailscale": 1, "xray": 2}
    clients.sort(
        key=lambda item: (
            kind_rank.get(str(item.get("kind")), 99),
            0 if bool(item.get("is_active")) else 1,
            str(item.get("display_name") or "").lower(),
        )
    )
    return clients


def filter_ui_clients(
    clients: list[dict[str, Any]],
    *,
    display_settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    settings = display_settings or get_ui_display_settings()
    hidden_subject_ids = {
        str(item).strip()
        for item in settings.get("hidden_subject_ids", [])
        if str(item).strip()
    }
    filtered: list[dict[str, Any]] = []
    for client in clients:
        if str(client.get("subject_id") or "").strip() in hidden_subject_ids:
            continue
        kind = str(client.get("kind") or "")
        inventory_role = str(client.get("inventory_role") or _inventory_role_for_kind(kind))
        visibility_key = {
            "lan_client": "lan",
            "external_network_source": "external_network_source",
            "vless_client": "vless_client",
        }.get(inventory_role, kind)
        if visibility_key in {"lan", "external_network_source", "vless_client"} and not _system_visible(settings, visibility_key):
            continue
        if not settings["show_inactive"] and not bool(client.get("is_active")):
            continue
        if inventory_role == "vless_client" and not settings["show_internal_vless"] and bool(client.get("is_internal")):
            continue
        filtered.append(client)
    return filtered


def _ui_client_stats(
    clients: list[dict[str, Any]],
    *,
    display_settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    hidden_subject_ids = {
        str(item).strip()
        for item in display_settings.get("hidden_subject_ids", [])
        if str(item).strip()
    }
    panel_clients: list[dict[str, Any]] = []
    counts = {
        "all": len(clients),
        "panel": 0,
        "lan_client": 0,
        "external_network_source": 0,
        "vless_client": 0,
        "vless_internal": 0,
    }
    for client in clients:
        kind = str(client.get("kind") or "")
        inventory_role = str(client.get("inventory_role") or _inventory_role_for_kind(kind))
        if inventory_role == "vless_client" and bool(client.get("is_internal")):
            counts["vless_internal"] += 1
        if inventory_role in counts and not (inventory_role == "vless_client" and bool(client.get("is_internal"))):
            counts[inventory_role] += 1

        if str(client.get("subject_id") or "").strip() in hidden_subject_ids:
            continue
        visibility_key = {
            "lan_client": "lan",
            "external_network_source": "external_network_source",
            "vless_client": "vless_client",
        }.get(inventory_role, kind)
        if visibility_key in {"lan", "external_network_source", "vless_client"} and not _system_visible(display_settings, visibility_key):
            continue
        if not display_settings["show_inactive"] and not bool(client.get("is_active")):
            continue
        if inventory_role == "vless_client" and not display_settings["show_internal_vless"] and bool(client.get("is_internal")):
            continue
        panel_clients.append(client)

    counts["panel"] = len(panel_clients)
    return panel_clients, counts


def _list_ui_client_presence() -> list[dict[str, Any]]:
    subscription_map = _subscription_client_map()

    with db_session() as connection:
        basic_rows = connection.execute(
            """
            SELECT subject_id, subject_type, subject_role, implementation_kind, is_active
            FROM subjects
            WHERE is_deleted = 0 AND subject_type IN ('lan', 'tailscale', 'tailscale_node')
            """
        ).fetchall()
        xray_rows = connection.execute(
            """
            SELECT s.subject_id, s.subject_role, s.implementation_kind, s.display_name, s.alias, s.is_active, sx.email
            FROM subjects AS s
            JOIN subject_xray AS sx ON sx.subject_id = s.subject_id
            WHERE s.is_deleted = 0
            """
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in basic_rows:
        subject_type = str(row["subject_type"] or "")
        kind = "tailscale" if subject_type == "tailscale_node" else subject_type
        subject_role = str(row["subject_role"] or _inventory_role_for_kind(kind))
        items.append(
            {
                "subject_id": str(row["subject_id"]),
                "kind": kind,
                "inventory_role": subject_role,
                "implementation_kind": str(row["implementation_kind"] or subject_type),
                "is_active": _row_bool(row, "is_active"),
                "is_internal": False,
            }
        )

    grouped_xray: dict[str, dict[str, Any]] = {}
    for row in xray_rows:
        email = str(row["email"] or "")
        if _xray_service_subject(email):
            continue
        if _xray_legacy_subscription_shadow(email, subscription_map):
            continue
        group = _xray_subscription_group(row)
        if group is not None:
            group_subject_id, _group_label = group
            subscription_client = subscription_map.get(_subscription_group_token(group_subject_id), {})
            bucket = grouped_xray.setdefault(
                group_subject_id,
                {
                    "subject_id": group_subject_id,
                    "display_name": _group_label,
                    "kind": "xray",
                    "inventory_role": str(row["subject_role"] or _inventory_role_for_kind("xray")),
                    "implementation_kind": str(row["implementation_kind"] or "xray"),
                    "is_active": False,
                    "is_internal": False,
                },
            )
            bucket["is_active"] = bool(bucket["is_active"]) or _row_bool(row, "is_active") or _subscription_client_recent(subscription_client)
            continue
        subscription_client: dict[str, Any] = {}
        items.append(
            {
                "subject_id": str(row["subject_id"]),
                "kind": "xray",
                "inventory_role": str(row["subject_role"] or _inventory_role_for_kind("xray")),
                "implementation_kind": str(row["implementation_kind"] or "xray"),
                "is_active": _row_bool(row, "is_active") or bool(subscription_client.get("last_seen_at")),
                "is_internal": _xray_internal(email),
            }
        )

    items.extend(
        bucket
        for bucket in grouped_xray.values()
        if not _xray_opaque_subscription_label(bucket.get("display_name"))
    )
    return items


def _ui_workspace_counts(*, display_settings: dict[str, Any]) -> dict[str, int]:
    hidden_subject_ids = {
        str(item).strip()
        for item in display_settings.get("hidden_subject_ids", [])
        if str(item).strip()
    }
    counts = {
        "all": 0,
        "panel": 0,
        "lan_client": 0,
        "external_network_source": 0,
        "vless_client": 0,
        "vless_internal": 0,
        "docker": 0,
        "host": 0,
        "fwrouter": 0,
    }

    for client in _list_ui_client_presence():
        counts["all"] += 1
        inventory_role = str(client.get("inventory_role") or "")
        if inventory_role == "vless_client" and bool(client.get("is_internal")):
            counts["vless_internal"] += 1
        if inventory_role in counts and not (inventory_role == "vless_client" and bool(client.get("is_internal"))):
            counts[inventory_role] += 1

        if str(client.get("subject_id") or "").strip() in hidden_subject_ids:
            continue
        visibility_key = {
            "lan_client": "lan",
            "external_network_source": "external_network_source",
            "vless_client": "vless_client",
        }.get(inventory_role, str(client.get("kind") or ""))
        if visibility_key in {"lan", "external_network_source", "vless_client"} and not _system_visible(display_settings, visibility_key):
            continue
        if not display_settings["show_inactive"] and not bool(client.get("is_active")):
            continue
        if inventory_role == "vless_client" and not display_settings["show_internal_vless"] and bool(client.get("is_internal")):
            continue
        counts["panel"] += 1

    return counts


def list_ui_settings_inventory(
    *,
    role: str = "all",
    query: str = "",
    limit: int = 200,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    normalized_role = _normalize_inventory_role(role)
    selected_kinds = set(KINDS_BY_INVENTORY_ROLE.get(normalized_role, set()))
    normalized_query = str(query or "").strip().lower()
    display_settings = get_ui_display_settings()
    total_map, month_map, month_breakdown_map = _traffic_maps()
    subscription_map = _subscription_client_map()
    if normalized_role != "all":
        include_client_kinds = bool(selected_kinds & {"lan", "tailscale", "tailscale_node", "xray"})
        include_system_kinds = bool(selected_kinds & {"docker", "host"})
    else:
        include_client_kinds = True
        include_system_kinds = True
    routing = get_routing_global_state() or {}
    global_effective_mode = str(routing.get("desired_mode") or routing.get("applied_mode") or "direct").upper()

    def traffic_payload(subject_id: str) -> dict[str, Any]:
        month_breakdown = month_breakdown_map.get(subject_id, {})
        return {
            "traffic_total_bytes": int(total_map.get(subject_id, 0)),
            "traffic_month_bytes": int(month_map.get(subject_id, 0)),
            "traffic_month": month_breakdown,
            "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
            "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
        }

    def mode_source_for(desired_mode: str | None) -> str:
        return "GLOBAL" if str(desired_mode or "").lower() == "global" else "ADMIN_LOCKED"

    def effective_mode_for(
        *,
        subject_id: str,
        desired_mode: str | None,
        applied_mode: str,
        user_override_modes: dict[str, str],
    ) -> str:
        if str(desired_mode or "").lower() != "global":
            return applied_mode
        override_mode = user_override_modes.get(subject_id)
        if override_mode:
            return str(override_mode).upper()
        return global_effective_mode

    def mode_source_with_user_override(
        *,
        subject_id: str,
        desired_mode: str | None,
        user_override_modes: dict[str, str],
    ) -> str:
        if str(desired_mode or "").lower() == "global" and subject_id in user_override_modes:
            return "USER_OVERRIDE"
        return mode_source_for(desired_mode)

    items: list[dict[str, Any]] = []
    with db_session() as connection:
        if include_client_kinds:
            wants_lan = (
                (normalized_role != "all" and "lan" in selected_kinds)
                or normalized_role == "all"
            )
            if wants_lan:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.subject_role, s.implementation_kind, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at, s.last_traffic_at,
                        sl.ip_address, sl.mac_address, sl.hostname
                    FROM subjects AS s
                    JOIN subject_lan AS sl ON sl.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                user_override_modes = _active_user_override_modes([str(row["subject_id"]) for row in rows])
                for row in rows:
                    subject_id = str(row["subject_id"])
                    desired = str(row["desired_mode"] or "global").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "global").upper()
                    items.append(
                        {
                            "subject_id": subject_id,
                            "inventory_role": str(row["subject_role"] or "lan_client"),
                            "kind": str(row["subject_role"] or "lan_client"),
                            "implementation_kind": str(row["implementation_kind"] or "lan"),
                            "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["ip_address"] or subject_id),
                            "alias": row["alias"],
                            "hostname": row["hostname"],
                            "ip_address": row["ip_address"],
                            "mac_address": row["mac_address"],
                            "mode_source": mode_source_with_user_override(
                                subject_id=subject_id,
                                desired_mode=row["desired_mode"],
                                user_override_modes=user_override_modes,
                            ),
                            "effective_mode": effective_mode_for(
                                subject_id=subject_id,
                                desired_mode=row["desired_mode"],
                                applied_mode=applied,
                                user_override_modes=user_override_modes,
                            ),
                            "committed_desired_mode": desired,
                            "desired_mode": desired,
                            "applied_mode": applied,
                            "apply_state": str(row["apply_state"] or "clean"),
                            "runtime_state": row["runtime_state"],
                            "is_active": _row_bool(row, "is_active"),
                            "is_internal": False,
                            "last_seen_at": row["last_seen_at"],
                            "last_traffic_at": row["last_traffic_at"],
                            **traffic_payload(subject_id),
                        }
                    )

            wants_external_network = (
                (normalized_role != "all" and bool(selected_kinds & {"tailscale", "tailscale_node"}))
                or normalized_role == "all"
            )
            if wants_external_network:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.subject_type, s.subject_role, s.implementation_kind, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at, s.last_traffic_at,
                        st.tailscale_ip, st.hostname, st.user_name, st.online
                    FROM subjects AS s
                    JOIN subject_tailscale AS st ON st.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                user_override_modes = _active_user_override_modes([str(row["subject_id"]) for row in rows])
                for row in rows:
                    subject_id = str(row["subject_id"])
                    desired = str(row["desired_mode"] or "global").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "global").upper()
                    implementation_kind = str(row["implementation_kind"] or row["subject_type"] or "tailscale")
                    items.append(
                        {
                            "subject_id": subject_id,
                            "inventory_role": str(row["subject_role"] or "external_network_source"),
                            "kind": str(row["subject_role"] or "external_network_source"),
                            "implementation_kind": implementation_kind,
                            "display_system_id": _display_system_id_for_external_network_source(implementation_kind),
                            "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["tailscale_ip"] or subject_id),
                            "alias": row["alias"],
                            "hostname": row["hostname"],
                            "ip_address": row["tailscale_ip"],
                            "mac_address": None,
                            "user_name": row["user_name"],
                            "online": _row_bool(row, "online"),
                            "mode_source": mode_source_with_user_override(
                                subject_id=subject_id,
                                desired_mode=row["desired_mode"],
                                user_override_modes=user_override_modes,
                            ),
                            "effective_mode": effective_mode_for(
                                subject_id=subject_id,
                                desired_mode=row["desired_mode"],
                                applied_mode=applied,
                                user_override_modes=user_override_modes,
                            ),
                            "committed_desired_mode": desired,
                            "desired_mode": desired,
                            "applied_mode": applied,
                            "apply_state": str(row["apply_state"] or "clean"),
                            "runtime_state": row["runtime_state"],
                            "is_active": _row_bool(row, "is_active"),
                            "is_internal": False,
                            "last_seen_at": row["last_seen_at"],
                            "last_traffic_at": row["last_traffic_at"],
                            **traffic_payload(subject_id),
                        }
                    )

            wants_vless = (
                (normalized_role != "all" and "xray" in selected_kinds)
                or normalized_role == "all"
            )
            if wants_vless:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.subject_role, s.implementation_kind, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at, s.last_traffic_at,
                        sx.client_id, sx.client_uuid, sx.email, sx.subscription_path,
                        sx.last_subscription_at, sx.enabled
                    FROM subjects AS s
                    JOIN subject_xray AS sx ON sx.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                grouped_xray: dict[str, dict[str, Any]] = {}
                for row in rows:
                    subject_id = str(row["subject_id"])
                    email = str(row["email"] or "")
                    if _xray_service_subject(email):
                        continue
                    if _xray_legacy_subscription_shadow(email, subscription_map):
                        continue
                    token = _localpart(email)
                    subscription_client: dict[str, Any] = {}
                    desired = str(row["desired_mode"] or "enabled").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "enabled").upper()
                    alias = str(row["alias"] or "").strip() or None
                    group = _xray_subscription_group(row)
                    if group is not None:
                        group_subject_id, group_label = group
                        subscription_client = subscription_map.get(_subscription_group_token(group_subject_id), {})
                        subscription_recent = _subscription_client_recent(subscription_client)
                        bucket = grouped_xray.setdefault(
                            group_subject_id,
                            {
                                "subject_id": group_subject_id,
                                "subject_ids": [],
                                "inventory_role": str(row["subject_role"] or "vless_client"),
                                "kind": str(row["subject_role"] or "vless_client"),
                                "implementation_kind": str(row["implementation_kind"] or "xray"),
                                "display_name": group_label,
                                "alias": group_label,
                                "email": email,
                                "client_id": None,
                                "client_uuid": None,
                                "subscription_path": None,
                                "subscription_client": subscription_client,
                                "desired_values": [],
                                "applied_values": [],
                                "apply_state_values": [],
                                "runtime_state_values": [],
                                "is_active": False,
                                "is_internal": False,
                                "is_human": False,
                                "enabled": False,
                                "last_seen_values": [],
                                "last_traffic_values": [],
                                "last_subscription_values": [],
                                "last_user_agent_values": [],
                                "traffic_total_bytes": 0,
                                "traffic_month_bytes": 0,
                                "traffic_month": {key: 0 for key in TRAFFIC_METRIC_KEYS},
                                "member_count": 0,
                                "is_aggregate": True,
                                "aggregate_kind": "xray_subscription",
                                "can_delete": False,
                            },
                        )
                        month_breakdown = month_breakdown_map.get(subject_id, {})
                        bucket["subject_ids"].append(subject_id)
                        bucket["member_count"] += 1
                        bucket["desired_values"].append(row["desired_mode"] or "enabled")
                        bucket["applied_values"].append(row["applied_mode"] or row["desired_mode"] or "enabled")
                        bucket["apply_state_values"].append(row["apply_state"] or "clean")
                        bucket["runtime_state_values"].append(row["runtime_state"])
                        bucket["is_active"] = bool(bucket["is_active"]) or _row_bool(row, "is_active") or subscription_recent
                        bucket["enabled"] = bool(bucket["enabled"]) or _row_bool(row, "enabled")
                        if subscription_client and not bucket["subscription_client"]:
                            bucket["subscription_client"] = subscription_client
                        bucket["last_seen_values"].append(subscription_client.get("last_seen_at") or row["last_seen_at"])
                        bucket["last_traffic_values"].append(row["last_traffic_at"])
                        bucket["last_subscription_values"].append(row["last_subscription_at"])
                        bucket["last_user_agent_values"].append(subscription_client.get("last_user_agent"))
                        bucket["traffic_total_bytes"] += int(total_map.get(subject_id, 0))
                        bucket["traffic_month_bytes"] += int(month_map.get(subject_id, 0))
                        for key in TRAFFIC_METRIC_KEYS:
                            bucket["traffic_month"][key] += int(month_breakdown.get(key, 0))
                        continue
                    items.append(
                        {
                            "subject_id": subject_id,
                            "inventory_role": str(row["subject_role"] or "vless_client"),
                            "kind": str(row["subject_role"] or "vless_client"),
                            "implementation_kind": str(row["implementation_kind"] or "xray"),
                            "display_name": alias or str(subscription_client.get("display_name") or "").strip() or str(row["display_name"] or "").strip() or token or str(row["client_id"] or subject_id),
                            "alias": alias,
                            "email": email,
                            "client_id": row["client_id"],
                            "client_uuid": row["client_uuid"],
                            "subscription_path": row["subscription_path"],
                            "subscription_client": subscription_client,
                            "mode_source": mode_source_for(row["desired_mode"]),
                            "effective_mode": applied,
                            "committed_desired_mode": desired,
                            "desired_mode": desired,
                            "applied_mode": applied,
                            "apply_state": str(row["apply_state"] or "clean"),
                            "runtime_state": row["runtime_state"],
                            "is_active": _row_bool(row, "is_active") or bool(subscription_client.get("last_seen_at")),
                            **_activity_state(
                                is_active=_row_bool(row, "is_active"),
                                last_seen_at=subscription_client.get("last_seen_at") or row["last_seen_at"],
                                last_traffic_at=row["last_traffic_at"],
                            ),
                            "is_internal": _xray_internal(email),
                            "is_human": _human_xray_email(email),
                            "enabled": _row_bool(row, "enabled"),
                            "last_seen_at": subscription_client.get("last_seen_at") or row["last_seen_at"],
                            "last_traffic_at": row["last_traffic_at"],
                            "last_subscription_at": row["last_subscription_at"],
                            "last_user_agent": subscription_client.get("last_user_agent"),
                            **traffic_payload(subject_id),
                        }
                    )
                for bucket in grouped_xray.values():
                    if _xray_opaque_subscription_label(bucket["display_name"]):
                        continue
                    subject_id = str(bucket["subject_id"])
                    month_breakdown = dict(bucket["traffic_month"])
                    group_is_active = bool(bucket["is_active"])
                    group_last_seen_at = _latest_text(bucket["last_seen_values"])
                    group_last_traffic_at = _latest_text(bucket["last_traffic_values"])
                    group_subscription_recent = _subscription_client_recent(bucket["subscription_client"])
                    items.append(
                        {
                            "subject_id": subject_id,
                            "subject_ids": list(bucket["subject_ids"]),
                            "inventory_role": str(bucket["inventory_role"] or "vless_client"),
                            "kind": str(bucket["inventory_role"] or "vless_client"),
                            "implementation_kind": str(bucket["implementation_kind"] or "xray"),
                            "display_name": bucket["display_name"],
                            "alias": bucket["alias"],
                            "email": bucket["email"],
                            "client_id": bucket["client_id"],
                            "client_uuid": bucket["client_uuid"],
                            "subscription_path": bucket["subscription_path"],
                            "subscription_client": bucket["subscription_client"],
                            "mode_source": "ADMIN_LOCKED",
                            "effective_mode": _xray_group_mode(bucket["applied_values"], "enabled"),
                            "committed_desired_mode": _xray_group_mode(bucket["desired_values"], "enabled"),
                            "desired_mode": _xray_group_mode(bucket["desired_values"], "enabled"),
                            "applied_mode": _xray_group_mode(bucket["applied_values"], "enabled"),
                            "apply_state": "failed" if "failed" in {str(item or "").lower() for item in bucket["apply_state_values"]} else "clean",
                            "runtime_state": _latest_text(bucket["runtime_state_values"]),
                            "is_active": group_is_active,
                            **_activity_state(
                                is_active=group_is_active,
                                last_seen_at=group_last_seen_at,
                                last_traffic_at=group_last_traffic_at,
                                subscription_recent=group_subscription_recent,
                                subscription_group=True,
                            ),
                            "is_internal": False,
                            "is_human": False,
                            "enabled": bool(bucket["enabled"]),
                            "last_seen_at": group_last_seen_at,
                            "last_traffic_at": group_last_traffic_at,
                            "last_subscription_at": _latest_text(bucket["last_subscription_values"]),
                            "last_user_agent": _latest_text(bucket["last_user_agent_values"]),
                            "traffic_total_bytes": int(bucket["traffic_total_bytes"]),
                            "traffic_month_bytes": int(bucket["traffic_month_bytes"]),
                            "traffic_month": month_breakdown,
                            "traffic_panel_metric_keys": _subject_traffic_metric_keys(subject_id, display_settings),
                            "traffic_panel_metrics": _panel_traffic_metrics(subject_id, month_breakdown, display_settings),
                            "member_count": int(bucket["member_count"]),
                            "is_aggregate": True,
                            "aggregate_kind": "xray_subscription",
                            "can_delete": False,
                        }
                    )

        if include_system_kinds:
            wants_docker = (
                (normalized_role != "all" and "docker" in selected_kinds)
                or normalized_role == "all"
            )
            if wants_docker:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.subject_role, s.implementation_kind, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at,
                        sd.container_name, sd.compose_project, sd.compose_service
                    FROM subjects AS s
                    JOIN subject_docker AS sd ON sd.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                for row in rows:
                    subject_id = str(row["subject_id"])
                    desired = str(row["desired_mode"] or "direct").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "direct").upper()
                    items.append(
                        {
                            "inventory_role": str(row["subject_role"] or "docker_runtime"),
                            "kind": str(row["subject_role"] or "docker_runtime"),
                            "implementation_kind": str(row["implementation_kind"] or "docker"),
                            "subject_id": subject_id,
                            "display_name": str(row["alias"] or row["display_name"] or row["container_name"] or subject_id),
                            "alias": str(row["alias"] or ""),
                            "ip_address": "",
                            "mac_address": "",
                            "email": "",
                            "hostname": str(row["container_name"] or row["display_name"] or subject_id),
                            "user_name": "",
                            "mode_source": "SYSTEM",
                            "effective_mode": applied,
                            "committed_desired_mode": desired,
                            "applied_mode": applied,
                            "desired_mode": desired,
                            "runtime_state": str(row["runtime_state"] or ""),
                            "is_active": _row_bool(row, "is_active"),
                            "is_internal": False,
                            "last_seen_at": str(row["last_seen_at"] or ""),
                            "visibility": "active" if _row_bool(row, "is_active") else "inactive",
                            "can_delete": not _row_bool(row, "is_active"),
                            "traffic_month_bytes": 0,
                            "traffic_total_bytes": 0,
                            "traffic_month": {},
                            "traffic_panel_metric_keys": list(DEFAULT_TRAFFIC_PANEL_KEYS),
                            "traffic_panel_metrics": [
                                {"key": key, "label": TRAFFIC_METRIC_LABELS[key], "bytes": 0}
                                for key in DEFAULT_TRAFFIC_PANEL_KEYS
                            ],
                        }
                    )

            wants_host = (
                (normalized_role != "all" and "host" in selected_kinds)
                or normalized_role == "all"
            )
            if wants_host:
                rows = connection.execute(
                    """
                    SELECT
                        s.subject_id, s.subject_role, s.implementation_kind, s.display_name, s.alias, s.desired_mode, s.applied_mode,
                        s.apply_state, s.runtime_state, s.is_active, s.last_seen_at,
                        sh.systemd_unit, sh.process_name
                    FROM subjects AS s
                    JOIN subject_host AS sh ON sh.subject_id = s.subject_id
                    WHERE s.is_deleted = 0
                    ORDER BY s.is_active DESC, COALESCE(s.last_seen_at, s.updated_at) DESC
                    LIMIT ?
                    """,
                    (max(limit * 2, limit),),
                ).fetchall()
                for row in rows:
                    subject_id = str(row["subject_id"])
                    desired = str(row["desired_mode"] or "direct").upper()
                    applied = str(row["applied_mode"] or row["desired_mode"] or "direct").upper()
                    name = str(row["alias"] or row["display_name"] or row["systemd_unit"] or row["process_name"] or subject_id)
                    items.append(
                        {
                            "inventory_role": str(row["subject_role"] or "host_runtime"),
                            "kind": str(row["subject_role"] or "host_runtime"),
                            "implementation_kind": str(row["implementation_kind"] or "host"),
                            "subject_id": subject_id,
                            "display_name": name,
                            "alias": str(row["alias"] or ""),
                            "ip_address": "",
                            "mac_address": "",
                            "email": "",
                            "hostname": name,
                            "user_name": "",
                            "mode_source": "SYSTEM",
                            "effective_mode": applied,
                            "committed_desired_mode": desired,
                            "applied_mode": applied,
                            "desired_mode": desired,
                            "runtime_state": str(row["runtime_state"] or ""),
                            "is_active": _row_bool(row, "is_active"),
                            "is_internal": False,
                            "last_seen_at": str(row["last_seen_at"] or ""),
                            "visibility": "active" if _row_bool(row, "is_active") else "inactive",
                            "can_delete": not _row_bool(row, "is_active"),
                            "traffic_month_bytes": 0,
                            "traffic_total_bytes": 0,
                            "traffic_month": {},
                            "traffic_panel_metric_keys": list(DEFAULT_TRAFFIC_PANEL_KEYS),
                            "traffic_panel_metrics": [
                                {"key": key, "label": TRAFFIC_METRIC_LABELS[key], "bytes": 0}
                                for key in DEFAULT_TRAFFIC_PANEL_KEYS
                            ],
                        }
                    )

    filtered: list[dict[str, Any]] = []
    for item in items:
        implementation_kind = str(item.get("implementation_kind") or "").lower()
        item_role = str(item.get("inventory_role") or "").lower()
        item["inventory_role"] = item_role
        item["kind"] = item_role
        if normalized_role != "all":
            if item_role != normalized_role:
                continue
        visibility_key = {
            "lan_client": "lan",
            "external_network_source": "external_network_source",
            "vless_client": "vless_client",
            "docker_runtime": "docker",
            "host_runtime": "host",
        }.get(item_role, implementation_kind)
        if visibility_key in {"lan", "external_network_source", "vless_client", "docker", "host"} and not _system_visible(display_settings, visibility_key):
            continue
        if item_role == "vless_client" and not display_settings["show_internal_vless"] and bool(item.get("is_internal")):
            continue
        if not include_inactive and not display_settings["show_inactive"] and not bool(item.get("is_active")):
            continue
        if normalized_query:
            haystack = "\n".join(
                [
                    str(item.get("display_name") or ""),
                    str(item.get("alias") or ""),
                    str(item.get("ip_address") or ""),
                    str(item.get("mac_address") or ""),
                    str(item.get("email") or ""),
                    str(item.get("hostname") or ""),
                    str(item.get("user_name") or ""),
                    str(item.get("subject_id") or ""),
                ]
            ).lower()
            if normalized_query not in haystack:
                continue
        filtered.append(item)
        if len(filtered) >= limit:
            break
    return filtered


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
