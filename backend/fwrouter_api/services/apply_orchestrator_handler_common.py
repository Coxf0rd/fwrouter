from __future__ import annotations

from typing import Any

from fwrouter_api.services.subject_taxonomy import subject_follows_global_mode

from fwrouter_api.adapters.mihomo import (
    DEFAULT_MIHOMO_ADAPTER as DEFAULT_MIHOMO_ADAPTER,
)
from fwrouter_api.services.apply_orchestrator_state import (
    _load_user_override_map as _load_user_override_map,
)
from fwrouter_api.services.external_vpn import (
    external_vpn_mihomo_reconcile_skip as external_vpn_mihomo_reconcile_skip,
)
from fwrouter_api.services.mihomo_config_paths import (
    subject_selector_name as subject_selector_name,
)
from fwrouter_api.services.mihomo_reconcile import (
    reconcile_mihomo_runtime as reconcile_mihomo_runtime,
)




def _reconcile_vpn_runtime_for_apply(routing: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    external_skip = external_vpn_mihomo_reconcile_skip()
    if external_skip is not None:
        return external_skip
    return reconcile_mihomo_runtime(routing=routing, job_id=job_id)


def _switch_subject_mihomo_selector(subject_id: str, server_id: str) -> dict[str, Any]:
    selector_name = subject_selector_name(subject_id)
    result = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector(
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
    if external_vpn_mihomo_reconcile_skip() is not None:
        return False
    desired_mode = str(subject.get("desired_mode") or "").strip().lower()
    effective_mode = desired_mode
    if subject_follows_global_mode(subject_type) and desired_mode == "global":
        user_override = _load_user_override_map().get(str(subject.get("subject_id")))
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

