from __future__ import annotations

import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.core.config import get_settings
from fwrouter_api.services.dataplane_live import applied_nft_markers_match_live, probe_live_global_mode
from fwrouter_api.services.dataplane_global import (
    read_applied_manifest,
    read_effective_rules_artifact,
    validate_global_mode_request,
)
from fwrouter_api.services.dataplane_status import (
    build_runtime_enforcement_state,
    get_dataplane_capability,
)
from fwrouter_api.services.artifacts import write_job_json_artifact
from fwrouter_api.services.jobs import JobLockConflictError, get_job, touch_job_running
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.mihomo_config import (
    mihomo_runtime_satisfies_routing,
    reconcile_mihomo_runtime,
)
from fwrouter_api.services.global_mode_profiles import (
    load_precompiled_global_mode_profile,
    materialize_precompiled_manifest,
)
from fwrouter_api.services.rules import (
    finalize_manual_rules_apply,
    get_manual_rules_texts,
    mark_rules_job_failed,
    mark_rules_job_running,
    prepare_manual_rules_candidate,
)
from fwrouter_api.services.custom_servers import VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
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
    USER_OVERRIDE_TTL_DAYS,
    USER_MODES,
    enrich_subject_with_effective_state,
    get_subject_with_effective_state,
    get_routing_snapshot,
)
from fwrouter_api.services.subjects import get_subject, list_subjects
from fwrouter_api.services.xray import materialize_xray_runtime_bindings


INTENT_SET_GLOBAL_MODE = "set_global_mode"
INTENT_SET_GLOBAL_SERVER_MODE = "set_global_server_mode"
INTENT_SET_SELECTIVE_DEFAULT = "set_selective_default"
INTENT_SET_SUBJECT_ADMIN_MODE = "set_subject_admin_mode"
INTENT_SET_SUBJECT_USER_MODE = "set_subject_user_mode"
INTENT_SET_SUBJECT_SERVER_OVERRIDE = "set_subject_server_override"
INTENT_CLEAR_SUBJECT_SERVER_OVERRIDE = "clear_subject_server_override"
INTENT_APPLY_MANUAL_RULES = "apply_manual_rules"
INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME = "repair_global_direct_runtime"

JOB_TYPE_APPLY_MUTATION = "apply_mutation"
LOCK_APPLY = "apply"
LOCK_RULES = "rules"
LOCK_RULES_APPLY = "apply+rules"
GLOBAL_ROUTING_DRIFT_CODE = "ACTIVE_DATAPLANE_MODE_MISMATCH"
GLOBAL_ARTIFACT_DRIFT_CODE = "APPLIED_MANIFEST_ROUTING_MISMATCH"
LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE = "LIVE_DATAPLANE_ARTIFACT_DRIFT"


def _lock_for_intent(intent: str) -> str:
    if intent == INTENT_APPLY_MANUAL_RULES:
        return LOCK_RULES_APPLY
    return LOCK_APPLY


def _base_result(
    *,
    intent: str,
    job_id: str,
    stage: str,
    ok: bool,
    requested_by: str,
    apply_id: str | None = None,
    code: str | None = None,
    message: str | None = None,
    runtime_state_unchanged: bool = True,
) -> dict[str, Any]:
    try:
        runtime_enforcement = build_runtime_enforcement_state()
        capability = get_dataplane_capability()
    except Exception as exc:
        write_technical_log(
            component="apply-orchestrator",
            level="warning",
            event_type="runtime_enforcement_probe_failed",
            message="Failed to collect runtime enforcement diagnostics while building mutation result.",
            details={
                "intent": intent,
                "job_id": job_id,
                "stage": stage,
                "error": str(exc),
            },
        )
        runtime_enforcement = {
            "dataplane_capability": "unknown",
            "capability": "unknown",
            "enforcement_level": "runtime_probe_failed",
            "traffic_enforcement_guaranteed": False,
        }
        capability = "unknown"
    return {
        "ok": ok,
        "intent": intent,
        "job_id": job_id,
        "apply_id": apply_id,
        "requested_by": requested_by,
        "stage": stage,
        "code": code,
        "message": message,
        "runtime_state_unchanged": runtime_state_unchanged,
        "enforcement_level": runtime_enforcement["enforcement_level"],
        "dataplane_capability": capability,
        "traffic_enforcement_guaranteed": runtime_enforcement["traffic_enforcement_guaranteed"],
    }


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
    return str(subject["subject_type"]) in {"lan", "tailscale", "tailscale_node"}


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


def _current_routing_drift(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_routing = dict(routing or get_routing_snapshot() or {})
    live_probe = probe_live_global_mode()
    live_artifact_consistency = _live_applied_nft_artifact_consistency()
    expected_mode = str(
        resolved_routing.get("applied_mode") or resolved_routing.get("desired_mode") or "direct"
    ).strip().lower()
    expected_selective_default = str(
        resolved_routing.get("selective_default") or "direct"
    ).strip().lower()
    live_mode = str(live_probe.get("mode") or "unknown").strip().lower()
    live_selective_default = str(
        live_probe.get("selective_default") or "direct"
    ).strip().lower()
    matches_intent = bool(
        live_probe.get("ok")
        and live_mode == expected_mode
        and (
            expected_mode != "selective"
            or live_selective_default == expected_selective_default
        )
    )
    live_artifact_matches = not bool(live_artifact_consistency.get("detected"))
    drift_detected = not matches_intent or not live_artifact_matches
    drift_code = None
    if not matches_intent:
        drift_code = GLOBAL_ROUTING_DRIFT_CODE
    elif not live_artifact_matches:
        drift_code = LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE
    return {
        "detected": drift_detected,
        "code": drift_code,
        "routing": resolved_routing,
        "expected_mode": expected_mode,
        "expected_selective_default": expected_selective_default,
        "live_probe": live_probe,
        "live_mode": live_mode,
        "live_selective_default": live_selective_default,
        "live_artifact_consistency": live_artifact_consistency,
    }


def _live_applied_nft_artifact_consistency() -> dict[str, Any]:
    settings = get_settings()
    dataplane_dir = settings.paths.generated_dir / "dataplane"
    applied_manifest_path = dataplane_dir / "applied-manifest.json"
    applied_nft_path = dataplane_dir / "applied.nft"
    if not applied_manifest_path.exists() or not applied_nft_path.exists():
        return {
            "detected": False,
            "checked": False,
            "reason": "applied_artifacts_missing",
            "applied_manifest_path": str(applied_manifest_path),
            "applied_nft_path": str(applied_nft_path),
        }

    consistency = applied_nft_markers_match_live(applied_nft_path)
    return {
        "detected": not bool(consistency.get("ok", True)),
        "checked": True,
        "applied_manifest_path": str(applied_manifest_path),
        "applied_nft_path": str(applied_nft_path),
        **consistency,
    }


def _applied_manifest_routing_drift(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_routing = dict(routing or get_routing_snapshot() or {})
    applied_manifest = read_applied_manifest()
    if not (
        isinstance(applied_manifest, dict)
        and isinstance(applied_manifest.get("routing_global_state"), dict)
    ):
        return {
            "detected": False,
            "code": None,
            "routing": resolved_routing,
            "applied_manifest_routing": None,
            "mismatches": {},
        }

    manifest_routing = dict(applied_manifest.get("routing_global_state"))

    routing_keys = (
        "desired_mode",
        "applied_mode",
        "selective_default",
        "server_mode",
        "desired_fixed_server_id",
        "applied_fixed_server_id",
    )
    mismatches = {
        key: {
            "routing": resolved_routing.get(key),
            "applied_manifest": manifest_routing.get(key),
        }
        for key in routing_keys
        if resolved_routing.get(key) != manifest_routing.get(key)
    }

    return {
        "detected": bool(mismatches),
        "code": GLOBAL_ARTIFACT_DRIFT_CODE if mismatches else None,
        "routing": resolved_routing,
        "applied_manifest_routing": manifest_routing,
        "mismatches": mismatches,
    }


def _log_routing_drift(*, intent: str, requested_by: str, drift: dict[str, Any]) -> None:
    if not drift.get("detected"):
        return
    details = {
        "intent": intent,
        "requested_by": requested_by,
        "code": drift.get("code") or GLOBAL_ROUTING_DRIFT_CODE,
        "routing": drift.get("routing"),
        "expected_mode": drift.get("expected_mode"),
        "expected_selective_default": drift.get("expected_selective_default"),
        "live_mode": drift.get("live_mode"),
        "live_selective_default": drift.get("live_selective_default"),
        "live_probe": drift.get("live_probe"),
        "live_artifact_consistency": drift.get("live_artifact_consistency"),
    }
    write_operational_log(
        event_type="routing_live_drift_detected",
        level="warning",
        message="Persisted global routing state does not match live dataplane mode.",
        details=details,
    )
    write_technical_log(
        component="apply-orchestrator",
        level="warning",
        event_type="routing_live_drift_detected",
        message="Persisted global routing state does not match live dataplane mode.",
        details=details,
    )


def _log_artifact_drift(*, intent: str, requested_by: str, drift: dict[str, Any]) -> None:
    if not drift.get("detected"):
        return
    details = {
        "intent": intent,
        "requested_by": requested_by,
        "code": drift.get("code") or GLOBAL_ARTIFACT_DRIFT_CODE,
        "routing": drift.get("routing"),
        "applied_manifest_routing": drift.get("applied_manifest_routing"),
        "mismatches": drift.get("mismatches"),
    }
    write_operational_log(
        event_type="routing_artifact_drift_detected",
        level="warning",
        message="Applied routing manifest does not match persisted global routing state.",
        details=details,
    )
    write_technical_log(
        component="apply-orchestrator",
        level="warning",
        event_type="routing_artifact_drift_detected",
        message="Applied routing manifest does not match persisted global routing state.",
        details=details,
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


def _run_pipeline_for_state(
    *,
    job_id: str,
    reason: str,
    input_data: dict[str, Any],
    routing: dict[str, Any],
    subjects: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra_payload = dict(extra or {})
    if not isinstance(extra_payload.get("core_bypass"), dict):
        extra_payload["core_bypass"] = get_core_bypass_state()

    return run_apply_pipeline(
        job_id=job_id,
        reason=reason,
        mode=ApplyMode.APPLY,
        input_data=input_data,
        manifest_state={
            "routing_global_state": routing,
            "subjects": subjects,
            "extra": extra_payload,
        },
    )


def _run_pipeline_for_manifest(
    *,
    job_id: str,
    reason: str,
    input_data: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    return run_apply_pipeline(
        job_id=job_id,
        reason=reason,
        mode=ApplyMode.APPLY,
        input_data=input_data,
        prebuilt_manifest=manifest,
    )


def _scoped_runtime_message(status: str) -> str:
    if status == "applied":
        return "Applied"
    if status == "pending_inactive_subject":
        return "Pending (subject inactive)"
    if status == "pending_not_vpn_path":
        return "Pending (not in VPN path)"
    if status == "pending_unresolved_server":
        return "Pending (server unresolved)"
    return f"Pending ({status})"


def _scoped_runtime_error_code(status: str) -> str | None:
    if status == "applied":
        return None
    return f"SCOPED_RUNTIME_{status.upper()}"


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


def _log_mutation_result(result: dict[str, Any]) -> None:
    dedupe_key = None
    cooldown_seconds = None
    if result.get("ok") and result.get("intent") == INTENT_SET_GLOBAL_MODE:
        routing = result.get("routing") if isinstance(result.get("routing"), dict) else {}
        dedupe_key = json.dumps(
            {
                "intent": result.get("intent"),
                "mode": routing.get("applied_mode") or routing.get("desired_mode"),
                "selective_default": routing.get("selective_default"),
                "server_id": routing.get("active_server_id") or routing.get("server_id"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cooldown_seconds = 20

    write_operational_log(
        event_type=f"mutation_{result['intent']}_{'success' if result['ok'] else 'failed'}",
        level="info" if result["ok"] else "error",
        message=result["message"] or f"Mutation {result['intent']} successful." if result["ok"] else result["message"] or f"Mutation {result['intent']} failed.",
        details=result,
        dedupe_key=dedupe_key,
        cooldown_seconds=cooldown_seconds,
    )


def _build_failure_result(
    *,
    intent: str,
    job_id: str,
    requested_by: str,
    stage: str,
    code: str,
    message: str,
    apply_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_result(
        intent=intent,
        job_id=job_id,
        stage=stage,
        ok=False,
        requested_by=requested_by,
        apply_id=apply_id,
        code=code,
        message=message,
        runtime_state_unchanged=True,
    )


def _build_success_result(
    *,
    intent: str,
    job_id: str,
    requested_by: str,
    stage: str,
    apply_result: dict[str, Any],
    details: dict[str, Any] | None = None,
    runtime_state_unchanged: bool = False,
) -> dict[str, Any]:
    result = _base_result(
        intent=intent,
        job_id=job_id,
        stage=stage,
        ok=True,
        requested_by=requested_by,
        apply_id=apply_result.get("apply_id"),
        runtime_state_unchanged=runtime_state_unchanged,
    )
    if details:
        result.update(details)
    return result


def _validate_subject_user_mode(subject: dict[str, Any], mode: str) -> dict[str, str] | None:
    subject_type = str(subject["subject_type"])
    if subject_type == "xray":
        return {
            "code": "SUBJECT_MODE_FORBIDDEN",
            "message": "User mode changes are not allowed for Xray subjects.",
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
    if subject_type in {"lan", "tailscale", "tailscale_node"} and desired_mode != "global":
        return {
            "code": "SUBJECT_MODE_ADMIN_LOCKED",
            "message": "User override is allowed only while admin mode is global.",
        }
    if subject_type == "xray" and desired_mode != "enabled":
        return {
            "code": "SUBJECT_MODE_ADMIN_LOCKED",
            "message": "User override is allowed only while Xray admin mode is enabled.",
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
    if subject_type not in {"lan", "tailscale", "tailscale_node", "xray", "host", "docker"}:
        return {
            "code": "SUBJECT_TYPE_NOT_SUPPORTED",
            "message": f"Server override is not supported for subject type: {subject_type}.",
        }

    if server_id == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
        if subject_type != "xray":
            return {
                "code": "SERVER_OVERRIDE_VPN_AUTO_XRAY_ONLY",
                "message": "Virtual vpn-auto override is supported only for Xray subjects.",
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


def _commit_manual_rules_apply(
    *,
    job_id: str,
    draft_text: str,
    effective_artifact: dict[str, Any],
    runtime_enforcement: dict[str, Any],
) -> dict[str, Any]:
    return finalize_manual_rules_apply(
        job_id=job_id,
        manual_active_text=draft_text,
        effective_artifact=effective_artifact,
        runtime_enforcement=runtime_enforcement,
    )


def _execute_set_global_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_set_global_mode as impl

    return impl(job, payload)


def _execute_set_global_server_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_set_global_server_mode as impl

    return impl(job, payload)


def _execute_set_selective_default(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_set_selective_default as impl

    return impl(job, payload)


def _execute_set_subject_admin_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_set_subject_admin_mode as impl

    return impl(job, payload)


def _execute_set_subject_user_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_set_subject_user_mode as impl

    return impl(job, payload)


def _execute_set_subject_server_override(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_set_subject_server_override as impl

    return impl(job, payload)


def _execute_clear_subject_server_override(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_clear_subject_server_override as impl

    return impl(job, payload)


def _execute_apply_manual_rules(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_apply_manual_rules as impl

    return impl(job, payload)


def _execute_repair_global_direct_runtime(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_repair_global_direct_runtime as impl

    return impl(job, payload)


def execute_apply_mutation(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import execute_apply_mutation as impl

    return impl(job)


class ApplyOrchestrator:
    """Thin facade around Wave 1 transactional mutation orchestration."""

    @staticmethod
    def submit(
        *,
        intent: str,
        payload: dict[str, Any],
        requested_by: str = "api",
        run_now: bool = True,
    ) -> dict[str, Any]:
        return submit_apply_mutation(
            intent=intent,
            payload=payload,
            requested_by=requested_by,
            run_now=run_now,
        )

    @staticmethod
    def run(
        *,
        intent: str,
        payload: dict[str, Any],
        requested_by: str = "api",
    ) -> dict[str, Any]:
        return run_apply_mutation(
            intent=intent,
            payload=payload,
            requested_by=requested_by,
        )


def submit_apply_mutation(
    *,
    intent: str,
    payload: dict[str, Any],
    requested_by: str = "api",
    run_now: bool = True,
) -> dict[str, Any]:
    manager = get_default_job_manager()
    try:
        job = manager.create(
            JOB_TYPE_APPLY_MUTATION,
            lock_key=_lock_for_intent(intent),
            requested_by=requested_by,
            input_data={
                "intent": intent,
                "payload": payload,
            },
        )
    except JobLockConflictError:
        raise

    if run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job
    else:
        job = manager.start_job(job["job_id"]) or job
    return job


def run_apply_mutation(
    *,
    intent: str,
    payload: dict[str, Any],
    requested_by: str = "api",
) -> dict[str, Any]:
    job = submit_apply_mutation(
        intent=intent,
        payload=payload,
        requested_by=requested_by,
        run_now=True,
    )
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    mutation = result.get("mutation") if isinstance(result, dict) else None
    if isinstance(mutation, dict):
        return mutation
    if job.get("status") == "running":
        return _build_failure_result(
            intent=intent,
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            stage="job",
            code="JOB_RUNNING",
            message="Mutation job is still running; poll job status for completion.",
        )

    return _build_failure_result(
        intent=intent,
        job_id=str(job["job_id"]),
        requested_by=requested_by,
        stage="job",
        code=job.get("error_code") or "JOB_FAILED",
        message=job.get("error_message") or "Mutation job failed.",
    )


def set_subject_mode(
    subject_id: str,
    mode: str,
    *,
    actor_scope: str = "admin",
    requested_by: str = "api",
) -> dict[str, Any]:
    intent = (
        INTENT_SET_SUBJECT_USER_MODE
        if actor_scope == "user"
        else INTENT_SET_SUBJECT_ADMIN_MODE
    )
    return run_apply_mutation(
        intent=intent,
        payload={"subject_id": subject_id, "mode": mode},
        requested_by=requested_by,
    )


def set_subject_admin_mode(
    subject_id: str,
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_SET_SUBJECT_ADMIN_MODE,
        payload={"subject_id": subject_id, "mode": mode},
        requested_by=requested_by,
    )


def set_subject_user_mode(
    subject_id: str,
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_SET_SUBJECT_USER_MODE,
        payload={"subject_id": subject_id, "mode": mode},
        requested_by=requested_by,
    )


def set_global_mode(
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_SET_GLOBAL_MODE,
        payload={"mode": mode},
        requested_by=requested_by,
    )


def reconcile_current_routing_if_drift(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    """Reapply the persisted routing intent only when live dataplane drift exists."""

    routing = get_routing_snapshot() or ensure_routing_global_state()
    drift = _current_routing_drift(routing=routing)
    if not drift.get("detected"):
        return {
            "ok": True,
            "action": "none",
            "drift_detected": False,
            "drift": drift,
            "routing": routing,
            "message": "Live dataplane matches persisted routing intent.",
        }

    mode = str(
        (routing or {}).get("desired_mode")
        or (routing or {}).get("applied_mode")
        or "direct"
    ).strip().lower()
    mutation = set_global_mode(mode, requested_by=requested_by)
    return {
        "ok": bool(mutation.get("ok")),
        "action": "reapply_global_mode",
        "drift_detected": True,
        "drift": drift,
        "routing": routing,
        "mutation": mutation,
        "message": (
            "Live dataplane drift detected; persisted routing intent was reapplied."
            if mutation.get("ok")
            else "Live dataplane drift detected, but reapply failed."
        ),
        "error_code": None if mutation.get("ok") else mutation.get("code"),
        "error_message": None if mutation.get("ok") else mutation.get("message"),
    }


def apply_global_mode_immediately(
    mode: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    """Apply global mode synchronously for startup/bootstrap recovery.

    Startup recovery must use the same job lifecycle as normal API mutations:
    queued -> running -> success/failed. Otherwise the apply can complete while
    the SQLite jobs row remains queued and keeps the apply lock stale.
    """

    return run_apply_mutation(
        intent=INTENT_SET_GLOBAL_MODE,
        payload={"mode": mode},
        requested_by=requested_by,
    )


def set_selective_default(
    selective_default: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_SET_SELECTIVE_DEFAULT,
        payload={"selective_default": selective_default},
        requested_by=requested_by,
    )


def apply_manual_rules(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return run_apply_mutation(
        intent=INTENT_APPLY_MANUAL_RULES,
        payload={},
        requested_by=requested_by,
    )


def repair_global_direct_runtime(
    *,
    requested_by: str = "api",
    run_now: bool = True,
) -> dict[str, Any]:
    return submit_apply_mutation(
        intent=INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME,
        payload={},
        requested_by=requested_by,
        run_now=run_now,
    )


def repair_global_direct_runtime_sync(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    return submit_apply_mutation(
        intent=INTENT_REPAIR_GLOBAL_DIRECT_RUNTIME,
        payload={},
        requested_by=requested_by,
        run_now=True,
    )
