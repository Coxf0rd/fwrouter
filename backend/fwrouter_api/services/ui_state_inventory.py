from __future__ import annotations

from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.ui_display_settings import _system_visible
from fwrouter_api.services.ui_state_common import *
from fwrouter_api.services.ui_state_logs import _ui_text_title
from fwrouter_api.services.ui_state_settings import get_ui_display_settings


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
                                {"key": key, "label": _ui_text_title("traffic.metric", key) or key, "bytes": 0}
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
                                {"key": key, "label": _ui_text_title("traffic.metric", key) or key, "bytes": 0}
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
