from __future__ import annotations

from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services.artifacts import atomic_write_json
from fwrouter_api.services.control_plane_transfer_common import (
    CONTROL_PLANE_SNAPSHOT_VERSION,
    CONTROL_PLANE_TABLES,
    _fetch_one,
    _fetch_rows,
    _json_loads_or_none,
    _snapshot_file_path,
    _utc_now_iso,
)
from fwrouter_api.services.rules import get_effective_rules, get_manual_rules_texts, get_rules_state, list_rules_metadata
from fwrouter_api.services.subscription import get_subscription_state
from fwrouter_api.services.subjects import get_subject, list_subjects


def _export_subjects() -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for summary in list_subjects(include_deleted=True, limit=1000):
        subject = get_subject(str(summary["subject_id"])) or summary
        exported.append(subject)
    return exported


def _redact_subscription_state(
    state: dict[str, Any],
    *,
    include_secrets: bool,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    redacted = dict(state)
    if not include_secrets and redacted.get("url"):
        redacted["url"] = None
        redacted["url_redacted"] = True
        warnings.append("subscription_url_redacted")
    else:
        redacted["url_redacted"] = False
    return redacted, warnings


def _redact_custom_https_proxy_rows(
    rows: list[dict[str, Any]],
    *,
    include_secrets: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    exported: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if include_secrets:
            item["credentials_redacted"] = False
        else:
            if item.get("username") or item.get("password"):
                warnings.append("custom_server_credentials_redacted")
            item["username"] = None
            item["password"] = None
            item["credentials_redacted"] = True
        exported.append(item)
    deduped_warnings = sorted(set(warnings))
    return exported, deduped_warnings


def _export_settings_rows() -> list[dict[str, Any]]:
    rows = _fetch_rows(
        """
        SELECT key, value_json, updated_at
        FROM settings
        ORDER BY key
        """
    )
    return [
        {
            "key": row["key"],
            "value": _json_loads_or_none(row["value_json"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _export_rules_bundle() -> dict[str, Any]:
    texts = get_manual_rules_texts()
    effective = get_effective_rules()
    return {
        "state": get_rules_state(),
        "metadata_rows": list_rules_metadata(),
        "content": {
            "manual_draft_text": texts["draft_text"],
            "manual_active_text": texts["active_text"],
            "static_direct_text": texts["static_direct_text"],
            "big_direct_text": texts["big_direct_text"],
            "big_vpn_text": texts["big_vpn_text"],
            "effective_json": effective["effective"],
            "effective_text": effective["effective_text"],
            "metadata_json": effective["metadata"],
        },
    }


def export_control_plane_snapshot(*, include_secrets: bool = False, write_file: bool = True) -> dict[str, Any]:
    initialize_database()
    subscription_state, redaction_warnings = _redact_subscription_state(
        get_subscription_state(),
        include_secrets=include_secrets,
    )
    custom_https_proxy_rows, custom_server_redaction_warnings = _redact_custom_https_proxy_rows(
        _fetch_rows(
            """
            SELECT
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
            FROM server_custom_https_proxy
            ORDER BY server_id
            """
        ),
        include_secrets=include_secrets,
    )

    snapshot = {
        "snapshot_version": CONTROL_PLANE_SNAPSHOT_VERSION,
        "exported_at": _utc_now_iso(),
        "app_version": get_settings().app_version,
        "export_options": {
            "include_secrets": include_secrets,
        },
        "tables": list(CONTROL_PLANE_TABLES),
        "state": {
            "settings": _export_settings_rows(),
            "modules": _fetch_rows(
                """
                SELECT
                    module_name,
                    desired_state,
                    lifecycle_mode,
                    runtime_state,
                    apply_state,
                    status_text,
                    error_code,
                    error_message,
                    updated_at
                FROM modules
                ORDER BY module_name
                """
            ),
            "routing_global_state": _fetch_one(
                """
                SELECT
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
                FROM routing_global_state
                WHERE id = 1
                """
            ),
            "subjects": _export_subjects(),
            "subject_user_overrides": _fetch_rows(
                """
                SELECT
                    subject_id,
                    override_mode,
                    override_until,
                    created_by,
                    updated_at
                FROM subject_user_overrides
                ORDER BY subject_id
                """
            ),
            "subject_server_overrides": _fetch_rows(
                """
                SELECT
                    subject_id,
                    selected_server_id,
                    selected_until,
                    apply_state,
                    error_code,
                    error_message,
                    updated_at
                FROM subject_server_overrides
                ORDER BY subject_id
                """
            ),
            "servers": _fetch_rows(
                """
                SELECT
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
                FROM servers
                ORDER BY server_name, server_id
                """
            ),
            "server_custom_https_proxy": custom_https_proxy_rows,
            "server_preferences": _fetch_rows(
                """
                SELECT
                    server_id,
                    vpn_auto,
                    global_list,
                    remembered_until,
                    manually_deleted_at,
                    updated_at
                FROM server_preferences
                ORDER BY server_id
                """
            ),
            "server_ping_state": _fetch_rows(
                """
                SELECT
                    server_id,
                    status,
                    last_ping_ms,
                    checked_at,
                    checked_by,
                    error_code,
                    error_message,
                    metadata_json
                FROM server_ping_state
                ORDER BY server_id
                """
            ),
            "subscription_state": subscription_state,
            "rules": _export_rules_bundle(),
        },
        "warnings": redaction_warnings + custom_server_redaction_warnings + [
            "inventory_rows_are_cached_snapshot_only",
            "runtime_apply_required_after_import",
        ],
    }

    file_path = None
    if write_file:
        file_path = _snapshot_file_path()
        atomic_write_json(file_path, snapshot)

    return {
        "snapshot": snapshot,
        "file_path": str(file_path) if file_path else None,
    }

