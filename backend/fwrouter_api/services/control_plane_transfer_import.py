from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.artifacts import atomic_write_json, atomic_write_text
from fwrouter_api.services.control_plane_transfer_common import (
    _detail_table_for_subject_type,
    _insert_rows,
    _state_from_snapshot,
)
from fwrouter_api.services.control_plane_transfer_validation import validate_control_plane_snapshot
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.rules import get_manual_rules_texts, get_rules_state
from fwrouter_api.services.runtime import get_scoped_egress_runtime_summary
from fwrouter_api.services.system_summary import build_system_summary


def _write_rules_files_from_snapshot(rules_snapshot: dict[str, Any]) -> dict[str, str]:
    current = get_manual_rules_texts()
    content = rules_snapshot.get("content") if isinstance(rules_snapshot.get("content"), dict) else {}
    effective_json = content.get("effective_json")
    metadata_json = content.get("metadata_json")

    atomic_write_text(current["draft_path"], str(content.get("manual_draft_text") or ""))
    atomic_write_text(current["active_path"], str(content.get("manual_active_text") or ""))
    atomic_write_text(current["static_direct_path"], str(content.get("static_direct_text") or ""))
    atomic_write_text(current["big_direct_path"], str(content.get("big_direct_text") or ""))
    atomic_write_text(current["big_vpn_path"], str(content.get("big_vpn_text") or ""))
    atomic_write_text(current["effective_text_path"], str(content.get("effective_text") or ""))
    atomic_write_json(current["effective_json_path"], effective_json if isinstance(effective_json, dict) else {})
    atomic_write_json(current["metadata_path"], metadata_json if isinstance(metadata_json, dict) else {})

    return {
        "manual_draft_path": str(current["draft_path"]),
        "manual_active_path": str(current["active_path"]),
        "static_direct_path": str(current["static_direct_path"]),
        "big_direct_path": str(current["big_direct_path"]),
        "big_vpn_path": str(current["big_vpn_path"]),
        "effective_json_path": str(current["effective_json_path"]),
        "effective_text_path": str(current["effective_text_path"]),
        "metadata_path": str(current["metadata_path"]),
    }


def _normalized_module_row(row: dict[str, Any], *, normalize_runtime_state: bool) -> dict[str, Any]:
    if not normalize_runtime_state:
        return dict(row)
    desired_state = str(row.get("desired_state") or "disabled")
    return {
        **row,
        "lifecycle_mode": str(row.get("lifecycle_mode") or "none"),
        "runtime_state": "not_configured",
        "apply_state": "pending" if desired_state == "enabled" else "clean",
        "status_text": "Imported from control-plane snapshot. Runtime apply/verify is required.",
        "error_code": None,
        "error_message": None,
    }


def _normalized_routing_row(row: dict[str, Any] | None, *, normalize_runtime_state: bool) -> dict[str, Any] | None:
    if row is None or not normalize_runtime_state:
        return row
    return {
        **row,
        "applied_mode": None,
        "applied_fixed_server_id": None,
        "apply_state": "pending",
        "error_code": None,
        "error_message": None,
    }


def _normalized_subject_row(row: dict[str, Any], *, normalize_runtime_state: bool) -> dict[str, Any]:
    normalized = dict(row)
    subject_id = str(normalized.get("subject_id") or "")
    subject_type = str(normalized.get("subject_type") or "")

    if subject_id == "fwrouter:global" or subject_type == "fwrouter":
        normalized["desired_mode"] = "direct"
        normalized["applied_mode"] = None if normalize_runtime_state else "direct"
        normalized["apply_state"] = "pending" if normalize_runtime_state else "clean"
        return normalized

    if not normalize_runtime_state:
        return normalized
    return {
        **normalized,
        "applied_mode": None,
        "apply_state": "pending",
    }


def _normalized_subject_server_override(row: dict[str, Any], *, normalize_runtime_state: bool) -> dict[str, Any]:
    subject_id = str(row.get("subject_id") or "")
    if subject_id == "fwrouter:global":
        return {}
    if not normalize_runtime_state:
        return dict(row)
    return {
        **row,
        "apply_state": "pending",
        "error_code": None,
        "error_message": None,
    }


def _normalized_subscription_state(row: dict[str, Any], *, normalize_runtime_state: bool) -> dict[str, Any]:
    if not normalize_runtime_state:
        return dict(row)
    normalized = dict(row)
    normalized["status"] = "idle" if normalized.get("url") else "not_configured"
    normalized["error_code"] = None
    normalized["error_message"] = None
    return normalized


def _normalized_rules_state(
    row: dict[str, Any],
    *,
    normalize_runtime_state: bool,
    file_paths: dict[str, str],
) -> dict[str, Any]:
    normalized = {
        **row,
        **file_paths,
    }
    if not normalize_runtime_state:
        return normalized
    normalized.update(
        {
            "last_apply_job_id": None,
            "last_update_job_id": None,
            "status": "pending",
            "error_code": None,
            "error_message": None,
        }
    )
    return normalized


def import_control_plane_snapshot(
    snapshot: dict[str, Any],
    *,
    normalize_runtime_state: bool = True,
) -> dict[str, Any]:
    initialize_database()
    validation = validate_control_plane_snapshot(snapshot)
    if not validation["ok"]:
        return {
            "ok": False,
            "validation": validation,
            "imported": False,
        }

    state = _state_from_snapshot(snapshot)
    rules_snapshot = state.get("rules") if isinstance(state.get("rules"), dict) else {}
    rules_file_paths = _write_rules_files_from_snapshot(rules_snapshot)

    modules = [
        _normalized_module_row(dict(row), normalize_runtime_state=normalize_runtime_state)
        for row in (state.get("modules") or [])
        if isinstance(row, dict)
    ]
    routing = _normalized_routing_row(
        state.get("routing_global_state") if isinstance(state.get("routing_global_state"), dict) else None,
        normalize_runtime_state=normalize_runtime_state,
    )
    subjects = [
        _normalized_subject_row(dict(row), normalize_runtime_state=normalize_runtime_state)
        for row in (state.get("subjects") or [])
        if isinstance(row, dict)
    ]
    subject_user_overrides = [
        dict(row) for row in (state.get("subject_user_overrides") or []) if isinstance(row, dict)
    ]
    subject_server_overrides = [
        _normalized_subject_server_override(dict(row), normalize_runtime_state=normalize_runtime_state)
        for row in (state.get("subject_server_overrides") or [])
        if isinstance(row, dict)
    ]
    subject_server_overrides = [
        row
        for row in subject_server_overrides
        if isinstance(row, dict) and str(row.get("subject_id") or "").strip()
    ]
    settings_rows = [dict(row) for row in (state.get("settings") or []) if isinstance(row, dict)]
    servers = [dict(row) for row in (state.get("servers") or []) if isinstance(row, dict)]
    custom_https_proxy_rows = [
        dict(row) for row in (state.get("server_custom_https_proxy") or []) if isinstance(row, dict)
    ]
    server_preferences = [
        dict(row) for row in (state.get("server_preferences") or []) if isinstance(row, dict)
    ]
    server_ping_state = [
        dict(row) for row in (state.get("server_ping_state") or []) if isinstance(row, dict)
    ]
    subscription_state = _normalized_subscription_state(
        dict(state.get("subscription_state") or {}),
        normalize_runtime_state=normalize_runtime_state,
    )
    rules_state = _normalized_rules_state(
        dict((rules_snapshot.get("state") if isinstance(rules_snapshot.get("state"), dict) else get_rules_state())),
        normalize_runtime_state=normalize_runtime_state,
        file_paths=rules_file_paths,
    )
    rules_metadata_rows = [
        dict(row) for row in (rules_snapshot.get("metadata_rows") or []) if isinstance(row, dict)
    ]

    with db_session() as connection:
        for table in (
            "subject_server_overrides",
            "subject_user_overrides",
            "subject_lan",
            "subject_tailscale",
            "subject_xray",
            "subject_docker",
            "subject_host",
            "subject_fwrouter",
            "routing_global_state",
            "server_ping_state",
            "server_preferences",
            "server_custom_https_proxy",
            "subjects",
            "servers",
            "subscription_state",
            "rules_metadata",
            "rules_state",
            "modules",
            "settings",
        ):
            connection.execute(f"DELETE FROM {table}")

        _insert_rows(
            connection,
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["key"],
                    json.dumps(row.get("value"), ensure_ascii=False, sort_keys=True),
                    row.get("updated_at"),
                )
                for row in settings_rows
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO modules (
                module_name,
                desired_state,
                lifecycle_mode,
                runtime_state,
                apply_state,
                status_text,
                error_code,
                error_message,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["module_name"],
                    row["desired_state"],
                    row.get("lifecycle_mode") or "none",
                    row["runtime_state"],
                    row["apply_state"],
                    row.get("status_text"),
                    row.get("error_code"),
                    row.get("error_message"),
                    row.get("updated_at"),
                )
                for row in modules
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                subject_role,
                implementation_kind,
                stable_key,
                display_name,
                alias,
                desired_mode,
                applied_mode,
                apply_state,
                runtime_state,
                is_active,
                is_deleted,
                first_seen_at,
                last_seen_at,
                last_traffic_at,
                inactive_since,
                deleted_at,
                metadata_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row["stored_subject_type"] if row.get("stored_subject_type") else row["subject_type"],
                    row.get("subject_role") or "unknown",
                    row.get("implementation_kind") or row.get("stored_subject_type") or row["subject_type"],
                    row["stable_key"],
                    row.get("display_name"),
                    row.get("alias"),
                    row["desired_mode"],
                    row.get("applied_mode"),
                    row["apply_state"],
                    row["runtime_state"],
                    1 if row.get("is_active") else 0,
                    1 if row.get("is_deleted") else 0,
                    row.get("first_seen_at"),
                    row.get("last_seen_at"),
                    row.get("last_traffic_at"),
                    row.get("inactive_since"),
                    row.get("deleted_at"),
                    json.dumps(row.get("metadata"), ensure_ascii=False, sort_keys=True)
                    if row.get("metadata") is not None
                    else None,
                    row.get("created_at"),
                    row.get("updated_at"),
                )
                for row in subjects
            ],
        )

        subject_detail_rows: dict[str, list[dict[str, Any]]] = {
            "subject_lan": [],
            "subject_tailscale": [],
            "subject_xray": [],
            "subject_docker": [],
            "subject_host": [],
            "subject_fwrouter": [],
        }
        for subject in subjects:
            detail = subject.get("detail")
            if not isinstance(detail, dict):
                continue
            table = _detail_table_for_subject_type(str(subject["subject_type"]))
            if table is None:
                continue
            subject_detail_rows[table].append({"subject_id": subject["subject_id"], **detail})

        _insert_rows(
            connection,
            """
            INSERT INTO subject_lan (
                subject_id,
                mac_address,
                ip_address,
                hostname,
                dhcp_hostname,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("mac_address"),
                    row.get("ip_address"),
                    row.get("hostname"),
                    row.get("dhcp_hostname"),
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_lan"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_tailscale (
                subject_id,
                node_id,
                tailscale_ip,
                hostname,
                user_name,
                online,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("node_id"),
                    row.get("tailscale_ip"),
                    row.get("hostname"),
                    row.get("user_name"),
                    1 if row.get("online") else 0,
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_tailscale"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_xray (
                subject_id,
                client_id,
                client_uuid,
                email,
                subscription_path,
                last_subscription_at,
                enabled,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("client_id"),
                    row.get("client_uuid"),
                    row.get("email"),
                    row.get("subscription_path"),
                    row.get("last_subscription_at"),
                    1 if row.get("enabled", 1) else 0,
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_xray"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_docker (
                subject_id,
                compose_project,
                compose_service,
                container_name,
                container_id,
                image_name,
                ip_address,
                network_name,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("compose_project"),
                    row.get("compose_service"),
                    row.get("container_name"),
                    row.get("container_id"),
                    row.get("image_name"),
                    row.get("ip_address"),
                    row.get("network_name"),
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_docker"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_host (
                subject_id,
                systemd_unit,
                listen_proto,
                listen_port,
                executable,
                process_name,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("systemd_unit"),
                    row.get("listen_proto"),
                    row.get("listen_port"),
                    row.get("executable"),
                    row.get("process_name"),
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_host"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_fwrouter (
                subject_id,
                component_name,
                source_json,
                updated_at
            )
            VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("component_name"),
                    json.dumps(row.get("source"), ensure_ascii=False, sort_keys=True)
                    if row.get("source") is not None
                    else None,
                    row.get("updated_at"),
                )
                for row in subject_detail_rows["subject_fwrouter"]
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                country_code,
                region,
                raw_json,
                inventory_state,
                first_seen_at,
                last_seen_at,
                missing_since,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["server_id"],
                    row["server_name"],
                    row.get("provider_name"),
                    row.get("country_code"),
                    row.get("region"),
                    row.get("raw_json"),
                    row["inventory_state"],
                    row.get("first_seen_at"),
                    row.get("last_seen_at"),
                    row.get("missing_since"),
                    row.get("updated_at"),
                )
                for row in servers
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO server_custom_https_proxy (
                server_id,
                proxy_type,
                host,
                port,
                username,
                password,
                tls,
                sni,
                skip_cert_verify,
                path,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["server_id"],
                    row.get("proxy_type", "http"),
                    row["host"],
                    row["port"],
                    row.get("username"),
                    row.get("password"),
                    row.get("tls", 1),
                    row.get("sni"),
                    row.get("skip_cert_verify", 0),
                    row.get("path"),
                    row.get("updated_at"),
                )
                for row in custom_https_proxy_rows
            ],
        )
        if routing is not None:
            connection.execute(
                """
                INSERT INTO routing_global_state (
                    id,
                    desired_mode,
                    applied_mode,
                    selective_default,
                    server_mode,
                    desired_fixed_server_id,
                    applied_fixed_server_id,
                    fixed_server_until,
                    active_auto_server_id,
                    apply_state,
                    error_code,
                    error_message,
                    updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (
                    routing["desired_mode"],
                    routing.get("applied_mode"),
                    routing["selective_default"],
                    routing["server_mode"],
                    routing.get("desired_fixed_server_id"),
                    routing.get("applied_fixed_server_id"),
                    routing.get("fixed_server_until"),
                    routing.get("active_auto_server_id"),
                    routing["apply_state"],
                    routing.get("error_code"),
                    routing.get("error_message"),
                    routing.get("updated_at"),
                ),
            )
        _insert_rows(
            connection,
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list,
                remembered_until,
                manually_deleted_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["server_id"],
                    row.get("vpn_auto", 0),
                    row.get("global_list", 1),
                    row.get("remembered_until"),
                    row.get("manually_deleted_at"),
                    row.get("updated_at"),
                )
                for row in server_preferences
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO server_ping_state (
                server_id,
                status,
                last_ping_ms,
                checked_at,
                checked_by,
                error_code,
                error_message,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["server_id"],
                    row.get("status", "unknown"),
                    row.get("last_ping_ms"),
                    row.get("checked_at"),
                    row.get("checked_by"),
                    row.get("error_code"),
                    row.get("error_message"),
                    row.get("metadata_json"),
                )
                for row in server_ping_state
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_user_overrides (
                subject_id,
                override_mode,
                override_until,
                created_by,
                updated_at
            )
            VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("override_mode"),
                    row.get("override_until"),
                    row.get("created_by"),
                    row.get("updated_at"),
                )
                for row in subject_user_overrides
            ],
        )
        _insert_rows(
            connection,
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            [
                (
                    row["subject_id"],
                    row.get("selected_server_id"),
                    row.get("selected_until"),
                    row.get("apply_state", "clean"),
                    row.get("error_code"),
                    row.get("error_message"),
                    row.get("updated_at"),
                )
                for row in subject_server_overrides
            ],
        )

        if subscription_state:
            connection.execute(
                """
                INSERT INTO subscription_state (
                    id,
                    url,
                    status,
                    last_refresh_at,
                    last_success_at,
                    server_inventory_updated_at,
                    error_code,
                    error_message,
                    metadata_json,
                    updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                """,
                (
                    subscription_state.get("url"),
                    subscription_state.get("status", "not_configured"),
                    subscription_state.get("last_refresh_at"),
                    subscription_state.get("last_success_at"),
                    subscription_state.get("server_inventory_updated_at"),
                    subscription_state.get("error_code"),
                    subscription_state.get("error_message"),
                    json.dumps(subscription_state.get("metadata"), ensure_ascii=False, sort_keys=True)
                    if subscription_state.get("metadata") is not None
                    else None,
                    subscription_state.get("updated_at"),
                ),
            )

        connection.execute(
            """
            INSERT INTO rules_state (
                id,
                manual_draft_path,
                manual_active_path,
                static_direct_path,
                big_direct_path,
                big_vpn_path,
                effective_json_path,
                effective_text_path,
                metadata_path,
                selective_default,
                last_apply_job_id,
                last_update_job_id,
                status,
                last_success_at,
                last_failed_at,
                error_code,
                error_message,
                updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """,
            (
                rules_state["manual_draft_path"],
                rules_state["manual_active_path"],
                rules_state["static_direct_path"],
                rules_state["big_direct_path"],
                rules_state["big_vpn_path"],
                rules_state["effective_json_path"],
                rules_state["effective_text_path"],
                rules_state["metadata_path"],
                rules_state["selective_default"],
                rules_state.get("last_apply_job_id"),
                rules_state.get("last_update_job_id"),
                rules_state["status"],
                rules_state.get("last_success_at"),
                rules_state.get("last_failed_at"),
                rules_state.get("error_code"),
                rules_state.get("error_message"),
                rules_state.get("updated_at"),
            ),
        )
        _insert_rows(
            connection,
            """
            INSERT INTO rules_metadata (
                ruleset_id,
                ruleset_type,
                version_name,
                source_url,
                active_path,
                downloaded_at,
                activated_at,
                status,
                last_success_at,
                last_failed_at,
                last_error_code,
                last_error_message,
                last_job_id,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["ruleset_id"],
                    row["ruleset_type"],
                    row.get("version_name"),
                    row.get("source_url"),
                    row.get("active_path"),
                    row.get("downloaded_at"),
                    row.get("activated_at"),
                    row.get("status"),
                    row.get("last_success_at"),
                    row.get("last_failed_at"),
                    row.get("last_error_code"),
                    row.get("last_error_message"),
                    row.get("last_job_id"),
                    json.dumps(row.get("metadata_json"), ensure_ascii=False, sort_keys=True)
                    if row.get("metadata_json") is not None
                    else None,
                )
                for row in rules_metadata_rows
            ],
        )

    scoped_egress = get_scoped_egress_runtime_summary()
    system_summary = build_system_summary()
    write_operational_log(
        event_type="control_plane_snapshot_imported",
        message="Control-plane snapshot imported into local backend state.",
        details={
            "normalize_runtime_state": normalize_runtime_state,
            "subjects_count": len(subjects),
            "servers_count": len(servers),
        },
    )
    return {
        "ok": True,
        "validation": validation,
        "imported": True,
        "normalize_runtime_state": normalize_runtime_state,
        "summary": {
            "subjects_count": len(subjects),
            "servers_count": len(servers),
            "modules_count": len(modules),
            "settings_count": len(settings_rows),
        },
        "rules_files": rules_file_paths,
        "post_import": {
            "scoped_egress": scoped_egress,
            "system_summary": system_summary,
        },
    }

