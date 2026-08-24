from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services.core_bypass import BYPASS_SETTINGS_KEY
from fwrouter_api.services.control_plane_transfer_common import (
    CONTROL_PLANE_TABLES,
    _parse_datetime,
    _state_from_snapshot,
)
from fwrouter_api.services.control_plane_transfer_validation import validate_control_plane_snapshot
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state
from fwrouter_api.services.rules import get_manual_rules_texts
from fwrouter_api.services.scoped_egress import (
    build_scoped_egress_diagnostics,
    build_scoped_egress_readiness,
    summarize_scoped_subjects,
)
from fwrouter_api.services.subject_policy import enrich_subject_with_effective_state


def _snapshot_bypass_state(settings_rows: list[dict[str, Any]]) -> dict[str, Any]:
    state = {
        "enabled": False,
        "updated_at": None,
        "updated_by": None,
        "reason": None,
        "previous_runtime": None,
    }
    for row in settings_rows:
        if str(row.get("key") or "") != BYPASS_SETTINGS_KEY:
            continue
        value = row.get("value")
        if isinstance(value, dict):
            state.update(value)
        break
    return state


def _snapshot_active_override(row: dict[str, Any], *, until_field: str, value_field: str) -> dict[str, Any] | None:
    if row.get(value_field) in {None, ""}:
        return None
    until_value = _parse_datetime(row.get(until_field))
    if until_value is not None and until_value <= datetime.now(UTC):
        return None
    return dict(row)


def _snapshot_routing(state: dict[str, Any]) -> dict[str, Any]:
    routing = state.get("routing_global_state")
    if isinstance(routing, dict):
        return dict(routing)
    return {
        "desired_mode": "direct",
        "applied_mode": None,
        "selective_default": "direct",
        "server_mode": "auto",
        "desired_fixed_server_id": None,
        "applied_fixed_server_id": None,
        "fixed_server_until": None,
        "active_auto_server_id": None,
        "apply_state": "pending",
        "error_code": None,
        "error_message": None,
        "updated_at": None,
    }


def _enriched_subjects_from_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    runtime_enforcement = build_runtime_enforcement_state()
    routing = _snapshot_routing(state)
    settings_rows = [dict(row) for row in (state.get("settings") or []) if isinstance(row, dict)]
    bypass = _snapshot_bypass_state(settings_rows)
    user_override_rows = {
        str(row["subject_id"]): active
        for row in (state.get("subject_user_overrides") or [])
        if isinstance(row, dict)
        if (active := _snapshot_active_override(row, until_field="override_until", value_field="override_mode")) is not None
    }
    server_override_rows = {
        str(row["subject_id"]): active
        for row in (state.get("subject_server_overrides") or [])
        if isinstance(row, dict)
        if (
            active := _snapshot_active_override(
                row,
                until_field="selected_until",
                value_field="selected_server_id",
            )
        )
        is not None
    }

    subjects = [
        enrich_subject_with_effective_state(
            dict(subject),
            routing=routing,
            user_override=user_override_rows.get(str(subject.get("subject_id") or "")),
            server_override=server_override_rows.get(str(subject.get("subject_id") or "")),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass,
        )
        for subject in (state.get("subjects") or [])
        if isinstance(subject, dict)
    ]

    scoped_summary = summarize_scoped_subjects(subjects)
    scoped_diagnostics = build_scoped_egress_diagnostics(
        summary=scoped_summary,
        runtime_enforcement=runtime_enforcement,
        bypass=bypass,
    )
    scoped_readiness = build_scoped_egress_readiness(
        diagnostics=scoped_diagnostics,
        runtime_enforcement=runtime_enforcement,
        bypass=bypass,
    )
    return {
        "subjects": subjects,
        "runtime_enforcement": runtime_enforcement,
        "bypass": bypass,
        "routing": routing,
        "scoped_egress": {
            "diagnostics": scoped_diagnostics,
            "readiness": scoped_readiness,
        },
    }


def plan_control_plane_import(
    snapshot: dict[str, Any],
    *,
    normalize_runtime_state: bool = True,
) -> dict[str, Any]:
    initialize_database()
    validation = validate_control_plane_snapshot(snapshot)
    state = _state_from_snapshot(snapshot)
    simulated = _enriched_subjects_from_snapshot(state)
    settings_rows = [dict(row) for row in (state.get("settings") or []) if isinstance(row, dict)]
    modules = [dict(row) for row in (state.get("modules") or []) if isinstance(row, dict)]
    subject_server_overrides = [
        dict(row) for row in (state.get("subject_server_overrides") or []) if isinstance(row, dict)
    ]
    subscription_state = dict(state.get("subscription_state") or {})
    rules_snapshot = state.get("rules") if isinstance(state.get("rules"), dict) else {}
    rules_content = rules_snapshot.get("content") if isinstance(rules_snapshot.get("content"), dict) else {}
    current_rules_paths = get_manual_rules_texts()

    enabled_modules = sum(1 for row in modules if str(row.get("desired_state") or "") == "enabled")
    subject_pending_apply_count = len([subject for subject in simulated["subjects"] if bool(subject.get("is_active"))])
    override_pending_apply_count = len(
        [
            row
            for row in subject_server_overrides
            if _snapshot_active_override(row, until_field="selected_until", value_field="selected_server_id") is not None
        ]
    )
    warnings = list(validation.get("warnings") or [])
    if normalize_runtime_state:
        warnings.append(
            {
                "code": "IMPORT_RUNTIME_NORMALIZATION_ENABLED",
                "message": "Import will reset runtime/apply state and require a fresh Linux-side apply.",
            }
        )

    return {
        "ok": validation["ok"],
        "validation": validation,
        "normalize_runtime_state": normalize_runtime_state,
        "import_actions": {
            "replace_tables": list(CONTROL_PLANE_TABLES),
            "restore_rules_files": [
                {
                    "name": "manual_draft_text",
                    "path": str(current_rules_paths["draft_path"]),
                    "has_content": bool(rules_content.get("manual_draft_text")),
                },
                {
                    "name": "manual_active_text",
                    "path": str(current_rules_paths["active_path"]),
                    "has_content": bool(rules_content.get("manual_active_text")),
                },
                {
                    "name": "effective_json",
                    "path": str(current_rules_paths["effective_json_path"]),
                    "has_content": isinstance(rules_content.get("effective_json"), dict),
                },
            ],
        },
        "post_import_expectations": {
            "routing_apply_required": normalize_runtime_state and bool(state.get("routing_global_state")),
            "modules_pending_apply_count": enabled_modules if normalize_runtime_state else 0,
            "subjects_pending_apply_count": subject_pending_apply_count if normalize_runtime_state else 0,
            "server_override_reapply_count": override_pending_apply_count if normalize_runtime_state else 0,
            "subscription_url_present": bool(subscription_state.get("url")),
            "subscription_url_redacted": bool(subscription_state.get("url_redacted")),
            "core_bypass_enabled": bool(simulated["bypass"].get("enabled")),
        },
        "scoped_egress": simulated["scoped_egress"],
        "runtime_enforcement": simulated["runtime_enforcement"],
        "warnings": warnings,
        "summary": {
            **(validation.get("summary") if isinstance(validation.get("summary"), dict) else {}),
            "active_subjects_count": sum(1 for subject in simulated["subjects"] if bool(subject.get("is_active"))),
            "tracked_scoped_subjects_count": len(
                simulated["scoped_egress"]["diagnostics"].get("bindings") or []
            ),
        },
    }

