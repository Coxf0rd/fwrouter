from __future__ import annotations

from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.ui_display_settings import _system_visible
from fwrouter_api.services.ui_state_common import *
from fwrouter_api.services.ui_state_settings import get_ui_display_settings


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
                json_extract(s.metadata_json, '$.detail.provider_ip') AS provider_ip,
                json_extract(s.metadata_json, '$.detail.tailscale_ip') AS legacy_provider_ip,
                json_extract(s.metadata_json, '$.detail.ip_address') AS ip_address,
                json_extract(s.metadata_json, '$.detail.hostname') AS hostname,
                json_extract(s.metadata_json, '$.detail.user_name') AS user_name,
                json_extract(s.metadata_json, '$.detail.online') AS online
            FROM subjects s
            WHERE s.is_deleted = 0
              AND s.subject_role = 'external_network_source'
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
                json_extract(s.metadata_json, '$.detail.client_id') AS client_id,
                json_extract(s.metadata_json, '$.detail.client_uuid') AS client_uuid,
                json_extract(s.metadata_json, '$.detail.email') AS email,
                json_extract(s.metadata_json, '$.detail.subscription_path') AS subscription_path,
                json_extract(s.metadata_json, '$.detail.last_subscription_at') AS last_subscription_at,
                json_extract(s.metadata_json, '$.detail.enabled') AS enabled
            FROM subjects s
            WHERE s.is_deleted = 0
              AND s.implementation_kind = 'xray'
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
                "kind": "external_network_source",
                "inventory_role": str(row["subject_role"] or "external_network_source"),
                "implementation_kind": str(row["implementation_kind"] or row["subject_type"] or "external_network_client"),
                "display_name": str(row["alias"] or row["display_name"] or row["hostname"] or row["provider_ip"] or row["ip_address"] or row["legacy_provider_ip"] or subject_id),
                "alias": row["alias"],
                "hostname": row["hostname"],
                "ip_address": row["ip_address"] or row["provider_ip"] or row["legacy_provider_ip"],
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
                    "kind": "vless_client",
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
                "kind": "vless_client",
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
                "kind": "vless_client",
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

    kind_rank = {"lan": 0, "external_network_source": 1, "vless_client": 2}
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
            WHERE is_deleted = 0
              AND (
                  subject_type = 'lan'
                  OR subject_role = 'external_network_source'
              )
            """
        ).fetchall()
        xray_rows = connection.execute(
            """
            SELECT
                s.subject_id,
                s.subject_role,
                s.implementation_kind,
                s.display_name,
                s.alias,
                s.is_active,
                json_extract(s.metadata_json, '$.detail.email') AS email
            FROM subjects AS s
            WHERE s.is_deleted = 0
              AND s.implementation_kind = 'xray'
            """
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in basic_rows:
        subject_type = str(row["subject_type"] or "")
        implementation_kind = str(row["implementation_kind"] or subject_type)
        kind = (
            "external_network_source"
            if str(row["subject_role"] or "") == "external_network_source"
            else subject_type
        )
        subject_role = str(row["subject_role"] or _inventory_role_for_kind(kind))
        items.append(
            {
                "subject_id": str(row["subject_id"]),
                "kind": kind,
                "inventory_role": subject_role,
                "implementation_kind": implementation_kind,
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
                    "kind": "vless_client",
                    "inventory_role": str(row["subject_role"] or _inventory_role_for_kind("explicit_external_client")),
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
                "kind": "vless_client",
                "inventory_role": str(row["subject_role"] or _inventory_role_for_kind("explicit_external_client")),
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
