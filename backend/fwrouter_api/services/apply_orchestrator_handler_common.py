from __future__ import annotations

from typing import Any

from fwrouter_api.services import apply_orchestrator as orchestrator
from fwrouter_api.services.subject_taxonomy import subject_follows_global_mode


def _reconcile_vpn_runtime_for_apply(routing: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    external_skip = orchestrator.external_vpn_mihomo_reconcile_skip()
    if external_skip is not None:
        return external_skip
    return orchestrator.reconcile_mihomo_runtime(routing=routing, job_id=job_id)


def _switch_subject_mihomo_selector(subject_id: str, server_id: str) -> dict[str, Any]:
    selector_name = orchestrator.subject_selector_name(subject_id)
    result = orchestrator.DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector(
        selector_name,
        server_id,
    )
    return {
        **result.to_dict(),
        "selector_name": selector_name,
        "requested_server_id": server_id,
    }


def _subject_needs_mihomo_selector_from_committed(
    subject: dict[str, Any],
    *,
    routing: dict[str, Any],
) -> bool:
    subject_type = str(subject.get("subject_type") or "").strip().lower()
    if orchestrator.external_vpn_mihomo_reconcile_skip() is not None:
        return False
    desired_mode = str(subject.get("desired_mode") or "").strip().lower()
    effective_mode = desired_mode
    if subject_follows_global_mode(subject_type) and desired_mode == "global":
        user_override = orchestrator._load_user_override_map().get(str(subject.get("subject_id")))
        if user_override is not None:
            effective_mode = str(user_override.get("override_mode") or "").strip().lower()
        else:
            effective_mode = str(
                routing.get("applied_mode")
                or routing.get("desired_mode")
                or "direct"
            ).strip().lower()
    return effective_mode in {"selective", "vpn"}


def _selective_default_artifact_drift_is_ignorable_for_global_direct(
    *,
    routing: dict[str, Any],
    artifact_drift: dict[str, Any],
) -> bool:
    if not artifact_drift.get("detected"):
        return True
    mode = str(routing.get("applied_mode") or routing.get("desired_mode") or "").strip().lower()
    if mode != "direct":
        return False
    mismatches = artifact_drift.get("mismatches")
    if not isinstance(mismatches, dict):
        return False
    return set(mismatches.keys()) == {"selective_default"}

