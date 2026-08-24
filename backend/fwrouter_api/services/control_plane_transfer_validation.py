from __future__ import annotations

from typing import Any

from fwrouter_api.services.control_plane_transfer_common import (
    CONTROL_PLANE_SNAPSHOT_VERSION,
    _detail_table_for_subject_type,
    _state_from_snapshot,
)


def validate_control_plane_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    state = _state_from_snapshot(snapshot)
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if snapshot.get("snapshot_version") != CONTROL_PLANE_SNAPSHOT_VERSION:
        errors.append(
            {
                "code": "SNAPSHOT_VERSION_UNSUPPORTED",
                "message": "Snapshot version is not supported by this backend.",
            }
        )

    subjects = state.get("subjects")
    if not isinstance(subjects, list):
        errors.append(
            {
                "code": "SNAPSHOT_SUBJECTS_INVALID",
                "message": "Snapshot subjects section must be a list.",
            }
        )
        subjects = []

    servers = state.get("servers")
    if not isinstance(servers, list):
        errors.append(
            {
                "code": "SNAPSHOT_SERVERS_INVALID",
                "message": "Snapshot servers section must be a list.",
            }
        )
        servers = []

    subscription_state = state.get("subscription_state")
    if isinstance(subscription_state, dict) and bool(subscription_state.get("url_redacted")):
        warnings.append(
            {
                "code": "SNAPSHOT_SUBSCRIPTION_URL_REDACTED",
                "message": "Subscription URL is redacted; import will not restore it.",
            }
        )

    custom_https_proxy_rows = state.get("server_custom_https_proxy")
    if custom_https_proxy_rows is not None and not isinstance(custom_https_proxy_rows, list):
        errors.append(
            {
                "code": "SNAPSHOT_CUSTOM_SERVERS_INVALID",
                "message": "Snapshot custom HTTPS proxy section must be a list.",
            }
        )
        custom_https_proxy_rows = []
    elif not isinstance(custom_https_proxy_rows, list):
        custom_https_proxy_rows = []

    redacted_custom_servers = sum(
        1
        for row in custom_https_proxy_rows
        if isinstance(row, dict) and bool(row.get("credentials_redacted"))
    )
    if redacted_custom_servers:
        warnings.append(
            {
                "code": "SNAPSHOT_CUSTOM_SERVER_CREDENTIALS_REDACTED",
                "message": "Some custom HTTPS proxy credentials are redacted; import will not restore them.",
                "count": redacted_custom_servers,
            }
        )

    unresolved_details = 0
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        subject_type = str(subject.get("subject_type") or "")
        if _detail_table_for_subject_type(subject_type) and not isinstance(subject.get("detail"), dict):
            unresolved_details += 1
    if unresolved_details:
        warnings.append(
            {
                "code": "SNAPSHOT_SUBJECT_DETAILS_PARTIAL",
                "message": "Some subject detail rows are missing in the snapshot.",
                "count": unresolved_details,
            }
        )

    routing = state.get("routing_global_state")
    if isinstance(routing, dict) and str(routing.get("desired_mode") or "") == "vpn" and not servers:
        warnings.append(
            {
                "code": "SNAPSHOT_VPN_WITHOUT_SERVERS",
                "message": "Routing desired_mode is vpn, but no server inventory rows are present.",
            }
        )

    rules = state.get("rules")
    if not isinstance(rules, dict):
        errors.append(
            {
                "code": "SNAPSHOT_RULES_INVALID",
                "message": "Snapshot rules section must be an object.",
            }
        )
    else:
        content = rules.get("content")
        if not isinstance(content, dict):
            errors.append(
                {
                    "code": "SNAPSHOT_RULES_CONTENT_INVALID",
                    "message": "Snapshot rules content section must be an object.",
                }
            )

    return {
        "ok": not errors,
        "snapshot_version": snapshot.get("snapshot_version"),
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "subjects_count": len(subjects),
            "servers_count": len(servers),
            "modules_count": len(state.get("modules") or []),
            "settings_count": len(state.get("settings") or []),
        },
        "defaults": {
            "normalize_runtime_state": True,
        },
    }

