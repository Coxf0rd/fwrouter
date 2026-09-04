from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.jobs import get_active_lock_lease, get_job_without_cleanup
from fwrouter_api.services.live_probe_cache import get_live_probe_cache
from fwrouter_api.services.subject_policy import list_subjects_with_effective_state
from fwrouter_api.services.subject_taxonomy import external_network_source_display_contract
from fwrouter_api.services.subject_groups import XRAY_SUBSCRIPTION_GROUP_PREFIX, xray_subscription_group_from_row
from fwrouter_api.services.ui_text import _ui_text_title


XRAY_INTERNAL_PREFIXES = ("sub-", "vpn-auto-")
XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS = 24 * 60 * 60
TRAFFIC_METRIC_KEYS = (
    "direct_rx_bytes",
    "direct_tx_bytes",
    "vpn_rx_bytes",
    "vpn_tx_bytes",
)
DEFAULT_TRAFFIC_PANEL_KEYS = ["vpn_rx_bytes", "vpn_tx_bytes"]
INVENTORY_ROLE_BY_KIND = {
    "lan": "lan_client",
    "external_network_client": "external_network_source",
    "explicit_external_client": "vless_client",
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
    "external_network_source": {"external_network_client", "tailscale", "tailscale_node"},
    "vless_client": {"explicit_external_client", "xray"},
    "docker_runtime": {"docker"},
    "host_runtime": {"host"},
    "router_core": {"fwrouter"},
}


DOMAIN_CATEGORY_BY_INVENTORY_ROLE = {
    "lan_client": "local_client",
    "vless_client": "external_client",
    "external_network_source": "external_network_source",
    "docker_runtime": "service",
    "host_runtime": "service",
    "router_core": "infrastructure",
}
IMPLEMENTATION_LABELS = {
    "lan": "LAN",
    "xray": "Xray/VLESS",
    "explicit_external_client": "Xray/VLESS",
    "tailscale": "Tailscale",
    "tailscale_node": "Tailscale",
    "docker": "Docker",
    "host": "Host",
    "fwrouter": "FWRouter",
    "mihomo": "Mihomo",
}


__all__ = ['XRAY_INTERNAL_PREFIXES', 'XRAY_SUBSCRIPTION_ACTIVE_WINDOW_SECONDS', 'TRAFFIC_METRIC_KEYS', 'DEFAULT_TRAFFIC_PANEL_KEYS', 'INVENTORY_ROLE_BY_KIND', 'INVENTORY_ROLE_ALIASES', 'KINDS_BY_INVENTORY_ROLE', 'DOMAIN_CATEGORY_BY_INVENTORY_ROLE', 'IMPLEMENTATION_LABELS', 'list_subjects_with_effective_state', '_inventory_role_for_kind', '_domain_category_for_inventory_role', '_implementation_label_for_kind', '_display_system_id_for_external_network_source', '_normalize_inventory_role', '_month_key', '_parse_ui_timestamp', '_subscription_group_token', '_subscription_client_recent', '_activity_state', '_normalize_traffic_metric_keys', '_subject_traffic_metric_keys', '_panel_traffic_metrics', '_traffic_maps', '_load_traffic_maps', '_subscription_client_map', '_load_subscription_client_map', '_list_effective_subjects_for_ui', '_effective_state_by_subject_for_ui', '_active_user_override_modes', '_human_xray_email', '_xray_internal', '_xray_service_subject', '_xray_legacy_subscription_shadow', '_localpart', '_xray_subscription_group', '_sum_month_breakdowns', '_latest_text', '_xray_group_mode', '_xray_opaque_subscription_label', '_row_bool', '_active_job', '_job_summary', '_system_subject_counts']


def _inventory_role_for_kind(kind: Any) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized in {"tailscale", "tailscale_node"}:
        return "external_network_source"
    if normalized == "xray":
        return "vless_client"
    return INVENTORY_ROLE_BY_KIND.get(normalized, "unknown")


def _domain_category_for_inventory_role(role: Any) -> str:
    normalized = _normalize_inventory_role(role)
    return DOMAIN_CATEGORY_BY_INVENTORY_ROLE.get(normalized, "local_client")


def _implementation_label_for_kind(kind: Any) -> str:
    normalized = str(kind or "").strip().lower()
    return IMPLEMENTATION_LABELS.get(normalized, str(kind or "").strip())


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
        reason = "profile_seen_24h"
        return {
            "activity_reason": reason,
            "activity_reason_label": _ui_text_title("inventory.activity", reason) or reason,
        }
    if is_active and last_traffic_at:
        reason = "traffic_seen"
        return {
            "activity_reason": reason,
            "activity_reason_label": _ui_text_title("inventory.activity", reason) or reason,
        }
    if is_active:
        reason = "runtime_active"
        return {
            "activity_reason": reason,
            "activity_reason_label": _ui_text_title("inventory.activity", reason) or reason,
        }
    if last_seen_at or last_traffic_at:
        reason = "stale_seen"
        return {
            "activity_reason": reason,
            "activity_reason_label": _ui_text_title("inventory.activity", reason) or reason,
        }
    reason = "unknown"
    return {
        "activity_reason": reason,
        "activity_reason_label": _ui_text_title("inventory.activity", reason) or reason,
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
            "label": _ui_text_title("traffic.metric", key) or key,
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
        "service": 0,
        "infrastructure": 0,
    }
    for row in rows:
        subject_type = str(row["subject_type"] or "")
        if subject_type in counts:
            value = int(row["count"] or 0)
            counts[subject_type] = value
            inventory_role = str(row["subject_role"] or _inventory_role_for_kind(subject_type))
            if inventory_role in counts:
                counts[inventory_role] = value
            domain_category = _domain_category_for_inventory_role(inventory_role)
            if domain_category in counts:
                counts[domain_category] += value
    return counts
