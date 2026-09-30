from __future__ import annotations

import hashlib
from typing import Any

from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.jobs import update_job_running_result
from fwrouter_api.services.subscription import (
    _server_from_metadata,
    _subscription_sources,
    compact_subscription_metadata,
    delete_subscription_source_intent,
    redact_subscription_public_value,
)
from fwrouter_api.services.subscription_pipeline import (
    apply_subscription_import_result,
    apply_subscription_refresh,
)
from fwrouter_api.services.events import write_operational_event


SUBSCRIPTION_REFRESH_OPERATION = "subscription_refresh"
SUBSCRIPTION_REFRESH_LOCK_KEY = "subscription_refresh"
SUBSCRIPTION_SOURCE_DELETE_OPERATION = "subscription_source_delete"
SUBSCRIPTION_REFRESH_STAGES = [
    "download",
    "parse",
    "persist",
    "prepare",
    "validate",
    "apply_runtime",
    "verify",
]


def _redact_subscription_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None

    public = dict(state)
    public["url_saved"] = bool(public.get("url"))
    public.pop("url", None)

    metadata = public.get("metadata")
    if isinstance(metadata, dict):
        public["metadata"] = compact_subscription_metadata(metadata, redact_urls=True)

    return redact_subscription_public_value(public)


def _redact_validation(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if validation is None:
        return None

    public = dict(validation)
    public["url_saved"] = bool(public.get("normalized_url"))
    public.pop("normalized_url", None)
    return redact_subscription_public_value(public)


def _redact_adapter_refresh(refresh: dict[str, Any] | None) -> dict[str, Any] | None:
    if refresh is None:
        return None

    public = dict(refresh)
    servers = public.pop("servers", []) or []
    public["servers_count"] = len(servers)

    metadata = public.get("metadata")
    if isinstance(metadata, dict):
        metadata_public = dict(metadata)
        metadata_public.pop("url", None)
        public["metadata"] = metadata_public

    return redact_subscription_public_value(public)


def _redact_subscription_refresh_result(result: dict[str, Any]) -> dict[str, Any]:
    public = dict(result)
    refresh = dict(public.get("refresh") or {})

    refresh["validation"] = _redact_validation(refresh.get("validation"))
    refresh["state"] = _redact_subscription_state(refresh.get("state"))
    refresh["refresh"] = _redact_adapter_refresh(refresh.get("refresh"))

    batch = refresh.get("batch")
    if isinstance(batch, dict):
        compact_items = []
        for item in batch.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_public = dict(item)
            item_public["refresh"] = _redact_adapter_refresh(item_public.get("refresh"))
            compact_items.append(item_public)
        refresh["batch"] = {**batch, "items": compact_items}

    public["refresh"] = refresh
    prepared_metadata = public.get("prepared_candidate_metadata")
    if isinstance(prepared_metadata, dict):
        public["prepared_candidate_metadata"] = {
            key: value
            for key, value in prepared_metadata.items()
            if not str(key).startswith("_")
        }
    candidate = public.get("candidate")
    if isinstance(candidate, dict):
        candidate_public = dict(candidate)
        candidate_public.pop("config", None)
        candidate_public.pop("_candidate_config", None)
        rules = candidate_public.pop("rules", None)
        handoff = candidate_public.pop("handoff_assignments", None)
        candidate_public.setdefault("rules_count", len(rules or []))
        candidate_public.setdefault("handoff_assignments_count", len(handoff or []))
        public["candidate"] = candidate_public
    return redact_subscription_public_value(public)


def subscription_refresh_stage(result: dict[str, Any]) -> str:
    """Map Phase 2 refresh internals to the long-operation stage contract."""

    if result.get("ok"):
        return "verify"

    stage = str(result.get("stage") or "").strip()
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    error_code = str(error.get("code") or result.get("error_code") or "").upper()

    if stage == "download_parse":
        if "PARSE" in error_code or "FORMAT" in error_code:
            return "parse"
        return "download"
    if stage == "config_validation":
        return "validate"
    if stage in {"candidate_validated", "prepare"}:
        return "prepare"
    if stage in {"applied", "already_current"}:
        return "verify"
    if stage in SUBSCRIPTION_REFRESH_STAGES:
        return stage
    return stage or "verify"


def _subscription_refresh_failure(
    *,
    job_id: str,
    stage: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    refresh = result.get("refresh") if isinstance(result.get("refresh"), dict) else {}
    refresh_error = refresh.get("error") if isinstance(refresh.get("error"), dict) else {}
    code = str(
        error.get("code")
        or refresh_error.get("code")
        or result.get("error_code")
        or "SUBSCRIPTION_REFRESH_FAILED"
    )
    message = str(
        error.get("message")
        or refresh_error.get("message")
        or result.get("error_message")
        or "Subscription refresh failed."
    )
    source = None
    validation = refresh.get("validation") if isinstance(refresh.get("validation"), dict) else {}
    if isinstance(validation, dict):
        source = {
            "url_saved": bool(validation.get("url_saved") or validation.get("normalized_url")),
        }

    return redact_subscription_public_value({
        "job_status": "failed",
        "job_id": job_id,
        "operation": SUBSCRIPTION_REFRESH_OPERATION,
        "stages": SUBSCRIPTION_REFRESH_STAGES,
        "stage": stage,
        "error_code": code,
        "error_message": message,
        "message": message,
        "source": source,
        "subscription": _redact_subscription_refresh_result(result),
    })


def run_subscription_refresh_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run full job-backed subscription refresh through runtime verification."""

    job_id = str(job["job_id"])
    update_job_running_result(
        job_id,
        result={
            "job_status": "running",
            "job_id": job_id,
            "operation": SUBSCRIPTION_REFRESH_OPERATION,
            "stages": SUBSCRIPTION_REFRESH_STAGES,
            "stage": "download",
            "message": "Downloading subscription sources.",
        },
    )
    result = apply_subscription_refresh()
    stage = subscription_refresh_stage(result)

    if not result.get("ok"):
        return _subscription_refresh_failure(job_id=job_id, stage=stage, result=result)

    public = _redact_subscription_refresh_result(result)
    return {
        "job_status": "success",
        "job_id": job_id,
        "operation": SUBSCRIPTION_REFRESH_OPERATION,
        "stages": SUBSCRIPTION_REFRESH_STAGES,
        "stage": "verify",
        "message": "Subscription refresh completed after Mihomo runtime verification.",
        "runtime_verified": True,
        "subscription": public,
        "candidate": public.get("candidate"),
        "config_validation": public.get("config_validation"),
        "promoted": bool(public.get("promoted")),
        "container_restarted": bool(public.get("container_restarted")),
        "applied": bool(public.get("applied")),
        "reconcile_action": public.get("reconcile_action"),
        "reconcile_reason": public.get("reconcile_reason"),
    }


def _source_delete_failure(*, job_id: str, source_ref: str, stage: str, code: str, message: str, result: dict[str, Any] | None = None, affected_server_id: str | None = None) -> dict[str, Any]:
    write_operational_event(
        severity="error", event_type="subscription_source_delete_apply_failed",
        event_code="subscription_source_delete_apply_failed",
        message="Saved subscription source deletion did not reach verified runtime state.",
        entity_type="subscription_source", entity_id=source_ref, job_id=job_id,
        details={"operation": SUBSCRIPTION_SOURCE_DELETE_OPERATION, "outcome": "failure", "source_ref": source_ref, "affected_server_id": affected_server_id, "stage": stage, "error_code": code, "error_message": message, "runtime_verified": False},
    )
    return {
        "job_status": "failed", "job_id": job_id,
        "operation": SUBSCRIPTION_SOURCE_DELETE_OPERATION, "stage": stage,
        "source": {"source_ref": source_ref, "deleted": bool(result and result.get("intent_deleted"))},
        "affected_server_id": affected_server_id,
        "runtime_verified": False, "error_code": code, "error_message": message,
        "message": message,
        "reconcile_pending": bool(result and result.get("intent_deleted")),
        "subscription": ({"stage": stage, "ok": False, "error": {"code": code, "message": message}} if result else None),
    }


def _run_subscription_source_delete_job(job: dict[str, Any]) -> dict[str, Any]:
    """Delete one exact source, then reconcile stored remaining inventory."""
    job_id = str(job["job_id"])
    source_ref = str((job.get("input") or {}).get("source_ref") or "")
    if not source_ref.startswith("src:") or len(source_ref) != 68:
        return _source_delete_failure(job_id=job_id, source_ref=source_ref, stage="intent", code="SUBSCRIPTION_SOURCE_REF_INVALID", message="Subscription source reference is invalid.")

    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.subscription import _upsert_subscription_servers, get_subscription_state
    from fwrouter_api.services.jobs import update_job_running_result

    with xray_writer_guard():
        intent = delete_subscription_source_intent(source_ref)
        if not intent.get("ok"):
            return _source_delete_failure(
                job_id=job_id, source_ref=source_ref, stage="preflight",
                code=str(intent.get("error_code") or "SUBSCRIPTION_SOURCE_DELETE_REJECTED"),
                message=str(intent.get("message") or "Subscription source deletion was rejected."),
                affected_server_id=str(intent.get("current_server_id") or "") or None,
            )

        update_job_running_result(job_id, result={
            "job_status": "running", "job_id": job_id,
            "operation": SUBSCRIPTION_SOURCE_DELETE_OPERATION,
            "stage": "inventory", "source": {"source_ref": source_ref, "deleted": True},
            "message": "Reconciling servers after saved subscription removal.",
        })

        state_after_delete = get_subscription_state()
        metadata = state_after_delete.get("metadata") if isinstance(state_after_delete.get("metadata"), dict) else {}
        remaining_sources = _subscription_sources(metadata)
        remaining_servers_by_url: dict[str, list[Any]] = {}
        remaining_servers: dict[str, Any] = {}
        for source in remaining_sources:
            url = str(source.get("url") or "").strip()
            parsed_servers = [
                server for payload in source.get("servers") or []
                if isinstance(payload, dict) and (server := _server_from_metadata(payload)) is not None
            ]
            if url:
                remaining_servers_by_url[url] = parsed_servers
            for server in parsed_servers:
                remaining_servers.setdefault(server.server_id, server)
        inventory = _upsert_subscription_servers(
            list(remaining_servers.values()), servers_by_url=remaining_servers_by_url
        )
        refresh_result = {
            "ok": True, "stage": "source_deleted", "state": get_subscription_state(),
            "inventory": inventory, "source": {"source_ref": source_ref, "deleted": True},
        }
        apply = apply_subscription_import_result(refresh_result)
        if not apply.get("ok"):
            error = apply.get("error") if isinstance(apply.get("error"), dict) else {}
            return _source_delete_failure(
                job_id=job_id, source_ref=source_ref,
                stage=str(apply.get("stage") or "apply_runtime"),
                code=str(error.get("code") or apply.get("error_code") or "SUBSCRIPTION_SOURCE_DELETE_APPLY_FAILED"),
                message="The source was removed, but runtime reconciliation did not complete. The previous verified runtime remains active and reconciliation is pending.",
                result={"intent_deleted": True, "apply": apply},
            )

        needs_auto_verification = bool(intent.get("current_auto_server_id") and intent.get("current_auto_server_id") in (intent.get("orphaned_server_ids") or []))
        selection = None
        if needs_auto_verification:
            from fwrouter_api.services.selector import get_vpn_auto_state
            selection_state = get_vpn_auto_state(read_only=True)
            logical = str(selection_state.get("active_auto_server_id") or "")
            logical_ok = bool(logical and selection_state.get("active_auto_server_valid"))
            candidate_map = dict(zip(
                [str(value) for value in selection_state.get("auto_selectable_candidate_ids") or []],
                [str(value) for value in selection_state.get("auto_selectable_candidate_target_names") or []],
            ))
            runtime_selectors = selection_state.get("selector_runtime") if isinstance(selection_state.get("selector_runtime"), dict) else {}
            effective_now = str(runtime_selectors.get("vpn_auto_now") or "").strip()
            expected_effective = candidate_map.get(logical) or logical
            effective_ok = bool(logical and effective_now and effective_now == expected_effective)
            selection = {
                "logical_server_id": logical or None,
                "effective_server_target": effective_now or None,
                "verified": bool(logical_ok and effective_ok),
            }
            if not logical_ok or not effective_ok:
                return _source_delete_failure(
                    job_id=job_id, source_ref=source_ref, stage="verify_selection",
                    code="SUBSCRIPTION_DELETE_AUTO_SELECTION_UNVERIFIED",
                    message="Subscription source was removed, but the replacement VPN-auto logical and effective target could not be verified.",
                    result={"intent_deleted": True, "apply": apply},
                )

        write_operational_event(
            severity="info", event_type="subscription_source_delete_applied",
            event_code="subscription_source_delete_applied",
            message="Saved subscription source removal completed after runtime verification.",
            entity_type="subscription_source", entity_id=source_ref, job_id=job_id,
            details={"operation": SUBSCRIPTION_SOURCE_DELETE_OPERATION, "outcome": "success", "source_ref": source_ref, "orphaned_servers_count": len(intent.get("orphaned_server_ids") or []), "runtime_verified": True},
        )
        public = {
            "ok": True, "stage": str(apply.get("stage") or "verified"),
            "reconcile_action": apply.get("reconcile_action"),
            "reconcile_reason": apply.get("reconcile_reason"),
        }
        return {
            "job_status": "success", "job_id": job_id,
            "operation": SUBSCRIPTION_SOURCE_DELETE_OPERATION, "stage": "verify",
            "message": "Saved subscription source deleted and runtime state verified.",
            "source": {"source_ref": source_ref, "deleted": True},
            "runtime_verified": True, "reconcile_pending": False,
            "orphaned_servers_count": len(intent.get("orphaned_server_ids") or []),
            "selection": selection, "subscription": public,
        }


def run_subscription_source_delete_job(job: dict[str, Any]) -> dict[str, Any]:
    """Keep unexpected provider/runtime diagnostics out of job DTOs."""
    job_id = str(job.get("job_id") or "")
    source_ref = str((job.get("input") or {}).get("source_ref") or "")
    try:
        return _run_subscription_source_delete_job(job)
    except Exception:
        state = None
        try:
            from fwrouter_api.services.subscription import _subscription_sources, get_subscription_state
            state = get_subscription_state()
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            still_saved = any(
                "src:" + hashlib.sha256(str(item.get("url") or "").encode("utf-8")).hexdigest() == source_ref
                for item in _subscription_sources(metadata)
            )
            if not still_saved and str(state.get("url") or ""):
                still_saved = "src:" + hashlib.sha256(str(state["url"]).encode("utf-8")).hexdigest() == source_ref
        except Exception:
            still_saved = True
        return _source_delete_failure(
            job_id=job_id, source_ref=source_ref,
            stage="apply_runtime" if not still_saved else "delete",
            code="SUBSCRIPTION_SOURCE_DELETE_INTERNAL_ERROR",
            message=("The source was removed, but runtime reconciliation did not complete. The previous verified runtime remains active and reconciliation is pending." if not still_saved else "The saved subscription source could not be deleted."),
            result={"intent_deleted": not still_saved, "apply": None},
        )


def register_subscription_refresh_handler(manager: JobManager) -> None:
    """Register/refresh the tracked subscription refresh job handler."""

    manager.register_handler(SUBSCRIPTION_REFRESH_OPERATION, run_subscription_refresh_job)
    manager.register_handler(SUBSCRIPTION_SOURCE_DELETE_OPERATION, run_subscription_source_delete_job)
