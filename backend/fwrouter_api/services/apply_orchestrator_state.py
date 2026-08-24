from __future__ import annotations

import json
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.artifacts import write_job_json_artifact
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.custom_servers import VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
from fwrouter_api.services.dataplane_global import (
    read_applied_manifest,
    read_effective_rules_artifact,
    validate_global_mode_request,
)
from fwrouter_api.services.dataplane_live import applied_nft_markers_match_live, probe_live_global_mode
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state, get_dataplane_capability
from fwrouter_api.services.external_vpn import external_vpn_mihomo_reconcile_skip
from fwrouter_api.services.global_mode_profiles import load_precompiled_global_mode_profile, materialize_precompiled_manifest
from fwrouter_api.services.jobs import get_job, touch_job_running
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.mihomo_config import (
    mihomo_runtime_satisfies_routing,
    reconcile_mihomo_runtime,
    reconcile_mihomo_selective_default_fast,
    subject_selector_name,
)
from fwrouter_api.services.rules import (
    effective_rules_with_selective_default,
    finalize_manual_rules_apply,
    get_manual_rules_texts,
    mark_rules_job_failed,
    mark_rules_job_running,
    prepare_manual_rules_candidate,
    sync_active_selective_default,
)
from fwrouter_api.services.servers import (
    clear_subject_server_override,
    ensure_routing_global_state,
    get_routing_global_state,
    get_server,
    get_subject_server_override,
    set_subject_server_override,
    update_subject_server_override_apply_status,
)
from fwrouter_api.services.subject_policy import (
    ADMIN_MODES_BY_SUBJECT_TYPE,
    USER_MODES,
    USER_OVERRIDE_TTL_DAYS,
    enrich_subject_with_effective_state,
    get_routing_snapshot,
    get_subject_with_effective_state,
)
from fwrouter_api.services.subject_taxonomy import (
    SERVER_OVERRIDE_SUBJECT_TYPES,
    explicit_external_client_allows_virtual_vpn_auto,
    explicit_external_client_runtime_binding,
    is_explicit_external_client_subject_type,
    subject_follows_global_mode,
)
from fwrouter_api.services.subjects import get_subject, list_subjects
from fwrouter_api.services.xray import materialize_xray_runtime_bindings
from fwrouter_api.services.apply_orchestrator_constants import *
from fwrouter_api.services.apply_orchestrator_results import _scoped_runtime_error_code, _scoped_runtime_message


def _load_user_override_map() -> dict[str, dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT subject_id, override_mode, override_until, created_by, updated_at
            FROM subject_user_overrides
            WHERE override_mode IS NOT NULL
              AND override_until > CURRENT_TIMESTAMP
            """
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


def _load_server_override_map() -> dict[str, dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
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
            WHERE selected_server_id IS NOT NULL
              AND selected_until > CURRENT_TIMESTAMP
            """
        ).fetchall()
    return {str(row["subject_id"]): dict(row) for row in rows}


def _load_subjects_with_overrides(
    *,
    routing: dict[str, Any],
    user_overrides: dict[str, dict[str, Any]],
    server_overrides: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    runtime_enforcement = build_runtime_enforcement_state()
    bypass_state = get_core_bypass_state()
    
    all_subjects = list_subjects(include_deleted=False, limit=1000)
    
    return [
        enrich_subject_with_effective_state(
            subject,
            routing=routing,
            user_override=user_overrides.get(str(subject["subject_id"])),
            server_override=server_overrides.get(str(subject["subject_id"])),
            runtime_enforcement=runtime_enforcement,
            bypass_state=bypass_state,
        )
        for subject in all_subjects
    ]


def _subject_follows_global(subject: dict[str, Any]) -> bool:
    return subject_follows_global_mode(str(subject["subject_type"]))


def _routing_mode(routing: dict[str, Any] | None) -> str:
    state = routing or {}
    return str(state.get("applied_mode") or state.get("desired_mode") or "direct")


def _persist_global_error(*, code: str, message: str) -> None:
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                apply_state = 'failed',
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (code, message),
        )


def _persist_subject_failure(subject_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE subjects
            SET
                apply_state = 'failed',
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (subject_id,),
        )


def _update_subject_apply_state(subject_id: str, apply_state: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE subjects
            SET
                apply_state = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (apply_state, subject_id),
        )


def _persist_rules_error(
    *,
    job_id: str,
    code: str,
    message: str,
    effective_artifact: dict[str, Any] | None = None,
) -> None:
    mark_rules_job_failed(
        job_id=job_id,
        code=code,
        message=message,
        update_type="manual_apply",
        effective_artifact=effective_artifact,
    )


def _find_subject_in_subjects(subjects: list[dict[str, Any]], subject_id: str) -> dict[str, Any] | None:
    for subject in subjects:
        if str(subject["subject_id"]) == subject_id:
            return subject
    return None


def _sync_subject_server_override_statuses(subjects: list[dict[str, Any]]) -> None:
    for subject in subjects:
        sid = str(subject["subject_id"])
        effective_state = subject.get("effective_state") if isinstance(subject, dict) else None
        runtime = effective_state.get("scoped_runtime") if isinstance(effective_state, dict) else None
        if not isinstance(runtime, dict):
            runtime = subject.get("scoped_runtime") if isinstance(subject, dict) else None
        if not isinstance(runtime, dict):
            status = str(subject.get("scoped_runtime_status") or "unknown")
        else:
            status = str(runtime.get("status") or "unknown")
        
        if status == "applied":
            update_subject_server_override_apply_status(sid, apply_state="clean")
        else:
            update_subject_server_override_apply_status(
                sid,
                apply_state="pending",
                error_code=_scoped_runtime_error_code(status),
                error_message=_scoped_runtime_message(status),
            )
