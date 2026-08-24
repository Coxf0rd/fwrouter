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


def _validate_subject_user_mode(subject: dict[str, Any], mode: str) -> dict[str, str] | None:
    subject_type = str(subject["subject_type"])
    if is_explicit_external_client_subject_type(subject_type):
        return {
            "code": "SUBJECT_MODE_FORBIDDEN",
            "message": "User mode changes are not allowed for explicit external client subjects.",
        }
    if subject_type not in ADMIN_MODES_BY_SUBJECT_TYPE:
        return {
            "code": "SUBJECT_TYPE_NOT_SUPPORTED",
            "message": f"User mode control is not supported for subject type: {subject_type}.",
        }
    if mode not in USER_MODES:
        return {
            "code": "SUBJECT_MODE_INVALID",
            "message": f"User mode must be one of: {', '.join(sorted(USER_MODES))}.",
        }

    desired_mode = str(subject.get("desired_mode") or "")
    if (
        subject_follows_global_mode(subject_type) and desired_mode != "global"
    ):
        return {
            "code": "SUBJECT_MODE_ADMIN_LOCKED",
            "message": "User override is allowed only while admin mode is global.",
        }
    if is_explicit_external_client_subject_type(subject_type) and desired_mode != "enabled":
        return {
            "code": "SUBJECT_MODE_ADMIN_LOCKED",
            "message": "User override is allowed only while explicit external client admin mode is enabled.",
        }

    return None


def _validate_subject_admin_mode(subject: dict[str, Any], mode: str) -> dict[str, str] | None:
    subject_type = str(subject["subject_type"])
    if subject_type == "fwrouter" and mode != "direct":
        return {
            "code": "FWROUTER_DIRECT_ONLY",
            "message": (
                "FWRouter own traffic is pinned to direct as an architectural invariant. "
                "Use a separate technical subject/service contour for any future special-case egress."
            ),
        }
    allowed_modes = ADMIN_MODES_BY_SUBJECT_TYPE.get(subject_type, set())
    if mode not in allowed_modes:
        return {
            "code": "SUBJECT_MODE_INVALID",
            "message": (
                f"Admin mode {mode} is not allowed for subject type {subject_type}. "
                f"Allowed: {', '.join(sorted(allowed_modes))}."
            ),
        }
    return None


def _validate_subject_server_override_request(
    subject: dict[str, Any],
    server_id: str,
) -> dict[str, Any] | None:
    subject_type = str(subject["subject_type"])
    if subject_type not in SERVER_OVERRIDE_SUBJECT_TYPES:
        return {
            "code": "SUBJECT_TYPE_NOT_SUPPORTED",
            "message": f"Server override is not supported for subject type: {subject_type}.",
        }

    if server_id == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
        if not explicit_external_client_allows_virtual_vpn_auto(subject_type):
            return {
                "code": "SERVER_OVERRIDE_VPN_AUTO_XRAY_ONLY",
                "message": "Virtual vpn-auto override is supported only for compatible explicit external clients.",
            }
        return None

    server = get_server(server_id)
    if server is None:
        return {
            "code": "SERVER_NOT_FOUND",
            "message": f"Server not found: {server_id}",
        }

    if server.get("inventory_state") != "active":
        return {
            "code": "SERVER_NOT_ACTIVE",
            "message": f"Server is not active: {server_id}",
            "server": server,
        }

    return None


def _commit_global_mode(*, mode: str) -> dict[str, Any]:
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = ?,
                applied_mode = ?,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (mode, mode),
        )
    return get_routing_global_state() or ensure_routing_global_state()


def _commit_global_server_mode(*, server_mode: str) -> dict[str, Any]:
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                server_mode = ?,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (server_mode,),
        )
    return get_routing_global_state() or ensure_routing_global_state()


def _commit_selective_default(*, selective_default: str) -> dict[str, Any]:
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                selective_default = ?,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (selective_default,),
        )
    return get_routing_global_state() or ensure_routing_global_state()


def _commit_repaired_global_runtime() -> dict[str, Any]:
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )
    return get_routing_global_state() or ensure_routing_global_state()


def _commit_subject_admin_mode(*, subject_id: str, mode: str) -> None:
    with db_session() as connection:
        if mode != "global":
            connection.execute(
                """
                DELETE FROM subject_user_overrides
                WHERE subject_id = ?
                """,
                (subject_id,),
            )
        connection.execute(
            """
            UPDATE subjects
            SET
                desired_mode = ?,
                applied_mode = ?,
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (mode, mode, subject_id),
        )


def _stage_subject_admin_mode(*, subject_id: str, mode: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE subjects
            SET
                desired_mode = ?,
                apply_state = 'pending',
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (mode, subject_id),
        )


def _commit_subject_user_mode(*, subject_id: str, mode: str, requested_by: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_user_overrides (subject_id, override_mode, override_until, created_by)
            VALUES (?, ?, datetime('now', '+' || ? || ' days'), ?)
            ON CONFLICT(subject_id) DO UPDATE SET
                override_mode = excluded.override_mode,
                override_until = excluded.override_until,
                created_by = excluded.created_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (subject_id, mode, USER_OVERRIDE_TTL_DAYS, requested_by),
        )
        connection.execute(
            """
            UPDATE subjects
            SET
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (subject_id,),
        )


def _clear_subject_user_mode(*, subject_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            DELETE FROM subject_user_overrides
            WHERE subject_id = ?
            """,
            (subject_id,),
        )
        connection.execute(
            """
            UPDATE subjects
            SET
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_id = ?
            """,
            (subject_id,),
        )

