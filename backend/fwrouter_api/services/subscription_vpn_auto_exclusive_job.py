from __future__ import annotations

import re
from typing import Any

from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.services.events import write_operational_event
from fwrouter_api.services.jobs import update_job_running_result
from fwrouter_api.services.vpn_auto_exclusive import (
    get_vpn_auto_exclusive_source_ref,
    save_vpn_auto_exclusive_source_ref,
    validate_exclusive_source,
)


VPN_AUTO_EXCLUSIVE_OPERATION = "subscription_vpn_auto_exclusive"
VPN_AUTO_EXCLUSIVE_STAGES = ["intent", "validate", "apply_runtime", "verify"]
SOURCE_REF_PATTERN = re.compile(r"^src:[0-9a-f]{64}$")


def _failed(
    job_id: str,
    source_ref: str | None,
    stage: str,
    code: str,
    *,
    enabled: bool,
    intent_saved: bool,
    retained: bool = True,
) -> dict[str, Any]:
    current = get_vpn_auto_exclusive_source_ref()
    details = {
        "source_ref": current,
        "requested_source_ref": source_ref,
        "enabled": enabled,
        "stage": stage,
        "error_code": code,
        "runtime_verified": False,
        "intent_saved": intent_saved,
        "last_good_retained": retained,
    }
    write_operational_event(
        severity="warning",
        event_type="vpn_auto_exclusive_unconfirmed",
        event_code="subscription.vpn_auto_exclusive_unconfirmed",
        message="Exclusive vpn-auto runtime convergence is unconfirmed.",
        entity_type="subscription_source",
        entity_id=source_ref,
        job_id=job_id,
        details=details,
    )
    return {
        "job_status": "failed",
        "job_id": job_id,
        "operation": VPN_AUTO_EXCLUSIVE_OPERATION,
        "stages": VPN_AUTO_EXCLUSIVE_STAGES,
        "stage": stage,
        "error_code": code,
        "error_message": "Exclusive vpn-auto runtime convergence is unconfirmed.",
        "source_ref": source_ref,
        "vpn_auto_exclusive": {"source_ref": current},
        "runtime_verified": False,
        "intent_saved": intent_saved,
        "last_good_retained": retained,
    }


def _exact_auto_targets() -> tuple[bool, dict[str, Any]]:
    from fwrouter_api.services.mihomo_config_proxies import _load_vpn_auto_proxy_names
    from fwrouter_api.services.selector import get_vpn_auto_state

    state = get_vpn_auto_state(read_only=True)
    expected = set(_load_vpn_auto_proxy_names()) | {"DIRECT"}
    observed = {
        str(target)
        for target in state.get("runtime_vpn_auto_targets") or []
        if str(target or "").strip()
    }
    return expected == observed, {
        "expected_target_count": len(expected),
        "observed_target_count": len(observed),
        "target_set_matches": expected == observed,
    }


def _runtime_verified_callback(
    *, enabled: bool, source_ref: str | None, operation_id: str,
    expected_selection_revision: int,
) -> dict[str, Any]:
    from fwrouter_api.services.selector import (
        select_vpn_auto_server,
    )

    targets_match, target_readback = _exact_auto_targets()
    if not targets_match:
        return {
            "ok": False,
            "error_code": "VPN_AUTO_EXCLUSIVE_TARGETS_READBACK_MISMATCH",
            "target_readback": target_readback,
        }
    selection: dict[str, Any] | None = None
    selection_before: dict[str, Any] | None = None

    def restore_selection_state() -> bool | None:
        if not enabled or selection_before is None:
            return None
        from fwrouter_api.services.xray_subscription_service import (
            _capture_generation_auto_selection,
            _restore_generation_auto_selection,
        )

        selection_after = _capture_generation_auto_selection()
        return _restore_generation_auto_selection(
            before=selection_before, after=selection_after, operation_id=None,
        )

    try:
        if enabled:
            from fwrouter_api.services.xray_subscription_service import _capture_generation_auto_selection

            selection_before = _capture_generation_auto_selection()
            selection = select_vpn_auto_server(
                apply=True,
                reason="vpn_auto_exclusive_source_changed",
                requested_by="api.subscription.vpn_auto_exclusive",
                origin="api",
                post_check=False,
                allow_provider_fallback=False,
                operation_id=operation_id,
                expected_selection_revision=expected_selection_revision,
            )
            if not selection.get("ok"):
                return {
                    "ok": False,
                    "error_code": str(selection.get("error_code") or "VPN_AUTO_EXCLUSIVE_SELECTION_UNCONFIRMED"),
                    "selection": selection,
                    "selection_state_restored": restore_selection_state(),
                    "target_readback": target_readback,
                }

        targets_match, target_readback = _exact_auto_targets()
        if not targets_match:
            return {
                "ok": False,
                "error_code": "VPN_AUTO_EXCLUSIVE_TARGETS_READBACK_MISMATCH",
                "selection": selection,
                "selection_state_restored": restore_selection_state(),
                "target_readback": target_readback,
            }
    except Exception:
        # Selection or its second readback may fail after mutating the active
        # server/provenance. Roll those two derived values back before asking
        # Mihomo reconciliation to restore the previous generation.
        try:
            selection_restored = restore_selection_state()
        except Exception:
            selection_restored = False
        return {
            "ok": False,
            "error_code": "VPN_AUTO_EXCLUSIVE_SELECTION_READBACK_FAILED",
            "selection": selection,
            "selection_state_restored": selection_restored,
            "target_readback": target_readback,
        }
    return {
        "ok": True,
        "source_ref": source_ref,
        "selection": selection,
        "target_readback": target_readback,
        "operation_id": operation_id,
        "selection_revision": (
            selection.get("selection_revision", expected_selection_revision)
            if isinstance(selection, dict) else expected_selection_revision
        ),
    }


def _run_vpn_auto_exclusive_job(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job["job_id"])
    payload = job.get("input") if isinstance(job.get("input"), dict) else {}
    source_ref = str(payload.get("source_ref") or "").strip()
    enabled = bool(payload.get("enabled"))

    update_job_running_result(
        job_id,
        result={
            "job_status": "running",
            "job_id": job_id,
            "operation": VPN_AUTO_EXCLUSIVE_OPERATION,
            "stages": VPN_AUTO_EXCLUSIVE_STAGES,
            "stage": "intent",
            "message": "Saving exclusive vpn-auto source intent.",
        },
    )

    if SOURCE_REF_PATTERN.fullmatch(source_ref) is None:
        return _failed(job_id, source_ref or None, "validate", "SUBSCRIPTION_SOURCE_REF_INVALID", enabled=enabled, intent_saved=False)
    previous = get_vpn_auto_exclusive_source_ref()
    if not enabled and previous != source_ref:
        return {
            "job_status": "success",
            "job_id": job_id,
            "operation": VPN_AUTO_EXCLUSIVE_OPERATION,
            "stages": VPN_AUTO_EXCLUSIVE_STAGES,
            "stage": "verify",
            "source_ref": source_ref,
            "vpn_auto_exclusive": {"source_ref": previous},
            "changed": False,
            "runtime_verified": None,
            "last_good_retained": True,
        }

    if enabled:
        validation = validate_exclusive_source(source_ref)
        if not validation.get("ok"):
            return _failed(job_id, source_ref, "validate", str(validation.get("error_code") or "SUBSCRIPTION_SOURCE_INVALID"), enabled=enabled, intent_saved=False)
        intent = save_vpn_auto_exclusive_source_ref(
            source_ref,
            requested_by="api.subscription.vpn_auto_exclusive",
        )
    else:
        intent = save_vpn_auto_exclusive_source_ref(
            None,
            requested_by="api.subscription.vpn_auto_exclusive",
        )
    if not intent.get("ok"):
        return _failed(job_id, source_ref, "validate", str(intent.get("error_code") or "SUBSCRIPTION_SOURCE_INVALID"), enabled=enabled, intent_saved=False)

    update_job_running_result(
        job_id,
        result={
            "job_status": "running",
            "job_id": job_id,
            "operation": VPN_AUTO_EXCLUSIVE_OPERATION,
            "stages": VPN_AUTO_EXCLUSIVE_STAGES,
            "stage": "apply_runtime",
            "source_ref": source_ref if enabled else None,
            "message": "Validating and applying Mihomo vpn-auto targets.",
        },
    )
    from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime

    reconcile = reconcile_mihomo_runtime(
        job_id=job_id,
        verification_callback=lambda **context: _runtime_verified_callback(
            enabled=enabled,
            source_ref=source_ref if enabled else None,
            **context,
        ),
    )
    verification = reconcile.get("verification_callback_result") if isinstance(reconcile.get("verification_callback_result"), dict) else {}
    if not reconcile.get("ok"):
        return _failed(
            job_id,
            source_ref if enabled else None,
            str(reconcile.get("stage") or "apply_runtime"),
            str(verification.get("error_code") or reconcile.get("error_code") or "MIHOMO_EXCLUSIVE_RECONCILE_FAILED"),
            enabled=enabled,
            intent_saved=True,
            retained=bool(reconcile.get(
                "last_good_retained",
                not bool((reconcile.get("promoted") or {}).get("promoted")),
            )),
        )

    current = get_vpn_auto_exclusive_source_ref()
    details = {
        "source_ref": current,
        "enabled": bool(current),
        "runtime_verified": True,
        "intent_saved": True,
        "last_good_retained": bool(reconcile.get("last_good_retained", True)),
        "changed": bool(intent.get("changed")),
    }
    write_operational_event(
        event_type="vpn_auto_exclusive_applied",
        event_code="subscription.vpn_auto_exclusive_applied",
        message="Exclusive vpn-auto source converged to the verified Mihomo runtime.",
        entity_type="subscription_source",
        entity_id=current or source_ref,
        job_id=job_id,
        details=details,
    )
    return {
        "job_status": "success",
        "job_id": job_id,
        "operation": VPN_AUTO_EXCLUSIVE_OPERATION,
        "stages": VPN_AUTO_EXCLUSIVE_STAGES,
        "stage": "verify",
        "source_ref": source_ref,
        "vpn_auto_exclusive": {"source_ref": current},
        "changed": bool(intent.get("changed")),
        "runtime_verified": True,
        "intent_saved": True,
        "last_good_retained": bool(reconcile.get("last_good_retained", True)),
        "apply": {
            "reconcile_action": reconcile.get("reconcile_action"),
            "reconcile_reason": reconcile.get("reconcile_reason"),
            "candidate": reconcile.get("candidate"),
            "config_validation": reconcile.get("config_validation"),
            "promoted": reconcile.get("promoted"),
            "container": reconcile.get("container"),
            "verification": verification,
        },
    }


def run_vpn_auto_exclusive_job(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.live_probe_cache import clear_live_probe_cache

    try:
        return _run_vpn_auto_exclusive_job(job)
    except Exception:
        payload = job.get("input") if isinstance(job.get("input"), dict) else {}
        source_ref = str(payload.get("source_ref") or "").strip() or None
        enabled = bool(payload.get("enabled"))
        current = get_vpn_auto_exclusive_source_ref()
        intent_saved = current == (source_ref if enabled else None)
        return _failed(
            str(job.get("job_id") or ""), source_ref,
            "apply_runtime", "VPN_AUTO_EXCLUSIVE_INTERNAL_ERROR",
            enabled=enabled, intent_saved=intent_saved, retained=False,
        )
    finally:
        clear_live_probe_cache()


def register_vpn_auto_exclusive_handler(manager: JobManager) -> None:
    manager.register_handler(VPN_AUTO_EXCLUSIVE_OPERATION, run_vpn_auto_exclusive_job)
