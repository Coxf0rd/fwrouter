from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any

from fwrouter_api.adapters.xray_common import xray_writer_guarded
from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.events import (
    create_event_context,
    write_audit_event,
)
from fwrouter_api.services.modules import fetch_modules
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.subjects import list_subjects


BYPASS_SETTINGS_KEY = "core.bypass"
JOB_TYPE_CORE_BYPASS = "core_bypass"
LOCK_KEY_CORE_BYPASS = "apply+module:core+selector+xray"
DEPENDENT_MODULES = ("vpn", "xray", "watchdog", "selector")


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_bypass_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "updated_at": None,
        "updated_by": None,
        "reason": None,
        "previous_runtime": None,
    }


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None

    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_bypass_setting() -> dict[str, Any] | None:
    with db_session() as connection:
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (BYPASS_SETTINGS_KEY,),
        ).fetchone()

    if row is None:
        return None
    return _json_loads(row["value_json"])


def _begin_core_bypass_apply() -> int:
    """Fence a bypass apply before rendering its owned manifest."""
    from fwrouter_api.services.core_intent import advance_core_intent_revision

    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        module = connection.execute(
            "SELECT desired_state, lifecycle_mode FROM modules WHERE module_name='core'",
        ).fetchone()
        if module is None or tuple(module) != ("enabled", "managed"):
            raise RuntimeError("CORE_INTENT_UNAVAILABLE")
        revision = advance_core_intent_revision(connection)
        connection.execute(
            "UPDATE modules SET apply_state='pending', error_code=NULL, error_message=NULL, "
            "status_text='Core bypass apply is in progress.', updated_at=CURRENT_TIMESTAMP "
            "WHERE module_name='core'",
        )
        return revision


def _save_bypass_setting(
    state: dict[str, Any],
    *,
    requested_by: str,
    job_id: str,
    expected_revision: int,
    expected_previous_enabled: bool,
    apply_id: str,
) -> bool:
    """Publish bypass intent and its matching apply under one Core CAS."""
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        from fwrouter_api.services.core_intent import read_core_intent_revision
        if read_core_intent_revision(connection) != expected_revision:
            return False
        module = connection.execute(
            "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
        ).fetchone()
        if module is None or tuple(module) != ("enabled", "managed", "pending"):
            return False
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key = ?",
            (BYPASS_SETTINGS_KEY,),
        ).fetchone()
        previous = _json_loads(row["value_json"]) if row is not None else None
        previous_enabled = bool((previous or {}).get("enabled"))
        if previous_enabled != expected_previous_enabled:
            return False
        latest = connection.execute(
            "SELECT status, core_intent_revision FROM apply_versions WHERE apply_id=?",
            (apply_id,),
        ).fetchone()
        if latest is None or latest["status"] != "applied" or latest["core_intent_revision"] != expected_revision:
            return False
        changed = previous_enabled != bool(state.get("enabled"))
        connection.execute(
            """
            INSERT INTO settings (key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (BYPASS_SETTINGS_KEY, _json_dumps(state)),
        )
        if state.get("enabled") is True:
            connection.execute(
                "UPDATE modules SET runtime_state='paused', apply_state='clean', "
                "status_text='FWRouter core is in intentional bypass mode; dataplane is direct-safe.', "
                "error_code=NULL, error_message=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE module_name='core' AND apply_state='pending'",
            )
        else:
            connection.execute(
                "UPDATE modules SET runtime_state='running', apply_state='pending', "
                "status_text='Core bypass disabled; waiting for fresh native Core proof.', "
                "error_code=NULL, error_message=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE module_name='core' AND apply_state='pending'",
            )
        if changed:
            enabled = bool(state.get("enabled"))
            action = "core_bypass_enabled" if enabled else "core_bypass_disabled"
            write_audit_event(
                actor=requested_by,
                actor_attribution="caller_supplied",
                source="core_bypass_admin_api",
                action=action,
                event_code="core.bypass_enabled" if enabled else "core.bypass_disabled",
                legacy_event_type=action,
                entity_type="configuration",
                entity_id=BYPASS_SETTINGS_KEY,
                previous_value={"enabled": previous_enabled},
                new_value={"enabled": enabled},
                context=create_event_context(job_id=job_id, entity_id=BYPASS_SETTINGS_KEY),
                details={"intent": "core_bypass"},
                connection=connection,
            )
    clear_live_probe_cache()
    return changed


def get_core_bypass_state() -> dict[str, Any]:
    saved = _load_bypass_setting()
    state = _default_bypass_state()
    if isinstance(saved, dict):
        state.update(saved)
    return state


def is_core_bypass_enabled() -> bool:
    return bool(get_core_bypass_state().get("enabled"))


def _snapshot_previous_runtime() -> dict[str, Any]:
    routing = ensure_routing_global_state()
    modules = {
        module["module_name"]: module
        for module in fetch_modules()
    }

    return {
        "captured_at": _utc_timestamp(),
        "routing": {
            "desired_mode": routing.get("desired_mode"),
            "applied_mode": routing.get("applied_mode"),
            "server_mode": routing.get("server_mode"),
            "active_auto_server_id": routing.get("active_auto_server_id"),
        },
        "modules": {
            module_name: {
                "desired_state": (modules.get(module_name) or {}).get("desired_state"),
                "runtime_state": (modules.get(module_name) or {}).get("runtime_state"),
                "apply_state": (modules.get(module_name) or {}).get("apply_state"),
                "status_text": (modules.get(module_name) or {}).get("status_text"),
            }
            for module_name in ("core", *DEPENDENT_MODULES)
        },
    }


def _load_subjects_for_apply() -> list[dict[str, Any]]:
    from fwrouter_api.services.subject_policy import enrich_subject_with_effective_state

    routing = ensure_routing_global_state()
    return [
        enrich_subject_with_effective_state(subject, routing=routing)
        for subject in list_subjects(include_deleted=False, limit=1000)
    ]


def _update_module_runtime(
    module_name: str,
    *,
    desired_state: str | None = None,
    runtime_state: str,
    apply_state: str,
    status_text: str,
    error_code: str | None = None,
    error_message: str | None = None,
    expected_module_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if module_name == "core":
        # This writer records bypass-recovery observations, not a Core authority
        # apply receipt. A successful observation stays pending until the
        # canonical owner settles it; failures retain their existing contract.
        apply_state = "failed" if apply_state == "failed" else "pending"
    updates = [
        "runtime_state = ?",
        "apply_state = ?",
        "status_text = ?",
        "error_code = ?",
        "error_message = ?",
        "updated_at = CURRENT_TIMESTAMP",
    ]
    values: list[Any] = [
        runtime_state,
        apply_state,
        status_text,
        error_code,
        error_message,
    ]

    if desired_state is not None:
        updates.insert(0, "desired_state = ?")
        values.insert(0, desired_state)

    where = "module_name = ?"
    values.append(module_name)
    if expected_module_state is not None:
        for column in ("desired_state", "lifecycle_mode", "runtime_state", "apply_state", "status_text"):
            where += f" AND {column} IS ?"
            values.append(expected_module_state.get(column))

    with db_session() as connection:
        if module_name == "core":
            from fwrouter_api.services.core_intent import advance_core_intent_revision
            advance_core_intent_revision(connection)
        connection.execute(
            f"""
            UPDATE modules
            SET {", ".join(updates)}
            WHERE {where}
            """,
            values,
        )
        row = connection.execute(
            """
            SELECT
                module_name,
                desired_state,
                runtime_state,
                apply_state,
                status_text,
                error_code,
                error_message,
                updated_at
            FROM modules
            WHERE module_name = ?
            """,
            (module_name,),
        ).fetchone()

    return dict(row) if row is not None else None


def _dependent_bypass_runtime(desired_state: str | None) -> str:
    return "paused" if desired_state == "enabled" else "stopped"


def _set_bypass_module_runtime() -> dict[str, dict[str, Any] | None]:
    modules = {
        module["module_name"]: module
        for module in fetch_modules()
    }
    updated: dict[str, dict[str, Any] | None] = {}

    updated["core"] = modules.get("core")

    for module_name in DEPENDENT_MODULES:
        current = modules.get(module_name) or {}
        desired_state = current.get("desired_state")
        runtime_state = _dependent_bypass_runtime(
            str(desired_state) if desired_state is not None else None
        )
        updated[module_name] = _update_module_runtime(
            module_name,
            runtime_state=runtime_state,
            apply_state="clean",
            status_text=(
                f"Module {module_name} is paused by FWRouter core bypass."
                if runtime_state == "paused"
                else f"Module {module_name} stays stopped while FWRouter core bypass is active."
            ),
        )

    return updated


def _restore_module_runtime(previous_runtime: dict[str, Any] | None) -> dict[str, dict[str, Any] | None]:
    modules = {
        module["module_name"]: module
        for module in fetch_modules()
    }
    snapshot_modules = (
        previous_runtime.get("modules", {})
        if isinstance(previous_runtime, dict)
        else {}
    )
    updated: dict[str, dict[str, Any] | None] = {}

    updated["core"] = modules.get("core")

    for module_name in DEPENDENT_MODULES:
        current = modules.get(module_name) or {}
        snapshot = snapshot_modules.get(module_name, {})
        if snapshot.get("desired_state") != current.get("desired_state"):
            # A newer dependent-module intent owns its runtime. Do not restore
            # the pre-bypass observation over that newer request.
            updated[module_name] = current or None
            continue
        default_runtime = _dependent_bypass_runtime(
            str(current.get("desired_state")) if current.get("desired_state") is not None else None
        )
        runtime_state = str(snapshot.get("runtime_state") or default_runtime)
        updated[module_name] = _update_module_runtime(
            module_name,
            runtime_state=runtime_state,
            apply_state="clean",
            status_text=(
                str(snapshot.get("status_text"))
                if snapshot.get("status_text")
                else f"Module {module_name} runtime restored after FWRouter core bypass."
            ),
            expected_module_state=current,
        )

    return updated


def _mark_core_failure(
    error_code: str, error_message: str, *, expected_revision: int,
) -> dict[str, Any] | None:
    """Record failure only while this bypass operation still owns Core intent."""
    from fwrouter_api.services.core_intent import advance_core_intent_revision, read_core_intent_revision

    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if read_core_intent_revision(connection) != expected_revision:
            return None
        row = connection.execute(
            "SELECT desired_state, lifecycle_mode FROM modules WHERE module_name='core'",
        ).fetchone()
        if row is None or tuple(row) != ("enabled", "managed"):
            return None
        advance_core_intent_revision(connection)
        bypass_row = connection.execute(
            "SELECT value_json FROM settings WHERE key=?", (BYPASS_SETTINGS_KEY,),
        ).fetchone()
        bypass_state = _json_loads(bypass_row["value_json"]) if bypass_row is not None else None
        runtime_state = "paused" if bool((bypass_state or {}).get("enabled")) else "failed"
        connection.execute(
            "UPDATE modules SET runtime_state=?, apply_state='failed', status_text=?, "
            "error_code=?, error_message=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE module_name='core' AND desired_state='enabled' AND lifecycle_mode='managed'",
            (runtime_state, error_message, error_code, error_message),
        )
        module = connection.execute(
            "SELECT * FROM modules WHERE module_name='core'",
        ).fetchone()
        return dict(module) if module is not None else None


def _run_core_bypass_apply(
    *,
    job_id: str,
    requested_by: str,
    reason: str,
    enabled: bool,
    core_intent_revision: int,
) -> dict[str, Any]:
    from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline

    routing = ensure_routing_global_state()
    subjects = _load_subjects_for_apply()
    return run_apply_pipeline(
        job_id=job_id,
        reason=reason,
        mode=ApplyMode.APPLY,
        input_data={
            "intent": "core_bypass",
            "action": "enable" if enabled else "disable",
        },
        manifest_state={
            "routing_global_state": routing,
            "subjects": subjects,
            "extra": {
                "core_bypass": {
                    "enabled": enabled,
                    "requested_by": requested_by,
                    "reason": reason,
                }
            },
        },
        core_intent_revision=core_intent_revision,
    )


@xray_writer_guarded
def enable_core_bypass(
    *,
    job_id: str,
    requested_by: str = "api",
    reason: str = "api_core_bypass_enable",
) -> dict[str, Any]:
    state = get_core_bypass_state()
    if state["enabled"]:
        return {
            "handler": JOB_TYPE_CORE_BYPASS,
            "job_id": job_id,
            "action": "enable",
            "already_enabled": True,
            "bypass": state,
        }

    previous_runtime = _snapshot_previous_runtime()
    try:
        core_revision = _begin_core_bypass_apply()
    except (RuntimeError, ValueError):
        return {
            "job_status": "failed", "error_code": "CORE_INTENT_UNAVAILABLE",
            "error_message": "Core intent could not be fenced for bypass apply.", "bypass": state,
        }
    apply_result = _run_core_bypass_apply(
        job_id=job_id,
        requested_by=requested_by,
        reason=reason,
        enabled=True,
        core_intent_revision=core_revision,
    )

    if not apply_result["ok"]:
        _mark_core_failure(
            "CORE_BYPASS_ENABLE_FAILED",
            apply_result["dataplane"]["error_message"]
            or apply_result["dataplane"]["message"]
            or "Failed to enable core bypass.",
            expected_revision=core_revision,
        )
        return {
            "job_status": "failed",
            "error_code": "CORE_BYPASS_ENABLE_FAILED",
            "error_message": apply_result["dataplane"]["error_message"]
            or apply_result["dataplane"]["message"]
            or "Failed to enable core bypass.",
            "bypass": state,
            "apply": apply_result,
        }

    updated_state = {
        "enabled": True,
        "updated_at": _utc_timestamp(),
        "updated_by": requested_by,
        "reason": reason,
        "previous_runtime": previous_runtime,
    }
    if not _save_bypass_setting(
        updated_state, requested_by=requested_by, job_id=job_id,
        expected_revision=core_revision, expected_previous_enabled=False,
        apply_id=str(apply_result.get("apply_id") or ""),
    ):
        return {
            "job_status": "failed", "error_code": "CORE_INTENT_STALE",
            "error_message": "Core intent changed during bypass apply; bypass publication was refused.",
            "bypass": get_core_bypass_state(), "apply": apply_result,
        }
    modules = _set_bypass_module_runtime()

    return {
        "handler": JOB_TYPE_CORE_BYPASS,
        "job_id": job_id,
        "action": "enable",
        "already_enabled": False,
        "bypass": get_core_bypass_state(),
        "apply": apply_result,
        "modules": modules,
    }


def _live_core_bypass_active() -> bool:
    try:
        completed = subprocess.run(
            ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return "fwrouter core bypass" in (completed.stdout or "")


@xray_writer_guarded
def disable_core_bypass(
    *,
    job_id: str,
    requested_by: str = "api",
    reason: str = "api_core_bypass_disable",
) -> dict[str, Any]:
    state = get_core_bypass_state()
    live_bypass_active = _live_core_bypass_active()
    if not state["enabled"] and not live_bypass_active:
        return {
            "handler": JOB_TYPE_CORE_BYPASS,
            "job_id": job_id,
            "action": "disable",
            "already_disabled": True,
            "bypass": state,
        }

    try:
        core_revision = _begin_core_bypass_apply()
    except (RuntimeError, ValueError):
        return {
            "job_status": "failed", "error_code": "CORE_INTENT_UNAVAILABLE",
            "error_message": "Core intent could not be fenced for bypass apply.", "bypass": state,
        }
    from fwrouter_api.services.core_intent import read_core_runtime_context
    runtime_context = read_core_runtime_context()
    apply_result = _run_core_bypass_apply(
        job_id=job_id,
        requested_by=requested_by,
        reason=reason,
        enabled=False,
        core_intent_revision=core_revision,
    )

    if not apply_result["ok"]:
        _set_bypass_module_runtime()
        _mark_core_failure(
            "CORE_BYPASS_DISABLE_FAILED",
            apply_result["dataplane"]["error_message"]
            or apply_result["dataplane"]["message"]
            or "Failed to disable core bypass.",
            expected_revision=core_revision,
        )
        return {
            "job_status": "failed",
            "error_code": "CORE_BYPASS_DISABLE_FAILED",
            "error_message": apply_result["dataplane"]["error_message"]
            or apply_result["dataplane"]["message"]
            or "Failed to disable core bypass.",
            "bypass": state,
            "apply": apply_result,
        }

    previous_runtime = (
        state.get("previous_runtime")
        if isinstance(state.get("previous_runtime"), dict)
        else None
    )
    cleared_state = {
        "enabled": False,
        "updated_at": _utc_timestamp(),
        "updated_by": requested_by,
        "reason": reason,
        "previous_runtime": None,
    }
    apply_id = str(apply_result.get("apply_id") or "")
    if not _save_bypass_setting(
        cleared_state, requested_by=requested_by, job_id=job_id,
        expected_revision=core_revision,
        expected_previous_enabled=bool(state.get("enabled")), apply_id=apply_id,
    ):
        return {
            "job_status": "failed", "error_code": "CORE_INTENT_STALE",
            "error_message": "Core intent changed during bypass apply; bypass publication was refused.",
            "bypass": get_core_bypass_state(), "apply": apply_result,
        }
    if runtime_context is None:
        _mark_core_failure(
            "CORE_NATIVE_PROOF_UNVERIFIED",
            "Core runtime context could not be fenced after bypass disable.",
            expected_revision=core_revision,
        )
        return {
            "job_status": "failed", "error_code": "CORE_NATIVE_PROOF_UNVERIFIED",
            "error_message": "Core runtime context could not be fenced after bypass disable.",
            "bypass": get_core_bypass_state(), "apply": apply_result,
        }
    from fwrouter_api.services.xray_subscription_service import _collect_legacy_admission_core_native_proof
    proof = _collect_legacy_admission_core_native_proof()
    from fwrouter_api.services.core_intent import settle_core_enable_after_proof
    if not settle_core_enable_after_proof(
        expected_revision=core_revision, apply_id=apply_id, proof=proof,
        expected_runtime_context=runtime_context,
    ):
        _mark_core_failure(
            "CORE_NATIVE_PROOF_UNVERIFIED",
            "Fresh Core native proof did not match the bypass-disable apply.",
            expected_revision=core_revision,
        )
        return {
            "job_status": "failed", "error_code": "CORE_NATIVE_PROOF_UNVERIFIED",
            "error_message": "Fresh Core native proof did not match the bypass-disable apply.",
            "bypass": get_core_bypass_state(), "apply": apply_result,
        }
    modules = _restore_module_runtime(previous_runtime)

    return {
        "handler": JOB_TYPE_CORE_BYPASS,
        "job_id": job_id,
        "action": "disable",
        "already_disabled": False,
        "bypass": get_core_bypass_state(),
        "apply": apply_result,
        "modules": modules,
    }


def core_bypass_handler(job: dict[str, Any]) -> dict[str, Any]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    action = str(input_data.get("action") or "").strip().lower()
    requested_by = str(job.get("requested_by") or "api")
    reason = str(input_data.get("reason") or f"job_{JOB_TYPE_CORE_BYPASS}_{action or 'unknown'}")

    if action == "enable":
        return enable_core_bypass(
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            reason=reason,
        )
    if action == "disable":
        return disable_core_bypass(
            job_id=str(job["job_id"]),
            requested_by=requested_by,
            reason=reason,
        )

    return {
        "job_status": "failed",
        "error_code": "CORE_BYPASS_ACTION_INVALID",
        "error_message": f"Unsupported core bypass action: {action}",
    }


def submit_core_bypass_job(
    *,
    action: str,
    requested_by: str = "api",
    reason: str | None = None,
    run_now: bool = True,
) -> dict[str, Any]:
    manager = get_default_job_manager()
    job = manager.create(
        JOB_TYPE_CORE_BYPASS,
        lock_key=LOCK_KEY_CORE_BYPASS,
        requested_by=requested_by,
        input_data={
            "action": action,
            "reason": reason or f"api_core_bypass_{action}",
        },
    )
    if run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job
    return job


__all__ = [
    "BYPASS_SETTINGS_KEY",
    "JOB_TYPE_CORE_BYPASS",
    "LOCK_KEY_CORE_BYPASS",
    "core_bypass_handler",
    "get_core_bypass_state",
    "is_core_bypass_enabled",
    "submit_core_bypass_job",
    "JobLockConflictError",
]
