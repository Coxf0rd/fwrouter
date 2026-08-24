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


def _facade_attr(name: str) -> Any:
    from fwrouter_api.services import apply_orchestrator as orchestrator

    return getattr(orchestrator, name)


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

    return _facade_attr("run_apply_pipeline")(
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
    return _facade_attr("run_apply_pipeline")(
        job_id=job_id,
        reason=reason,
        mode=ApplyMode.APPLY,
        input_data=input_data,
        prebuilt_manifest=manifest,
    )


def materialize_explicit_external_client_runtime_bindings(
    subject_type: str,
    *,
    requested_by: str,
    prepare_mihomo_handoff: bool,
) -> dict[str, Any]:
    runtime_binding = explicit_external_client_runtime_binding(subject_type)
    if runtime_binding == "xray_runtime_bindings":
        return materialize_xray_runtime_bindings(
            requested_by=requested_by,
            prepare_mihomo_handoff=prepare_mihomo_handoff,
        )
    return {
        "ok": False,
        "error_code": "EXPLICIT_CLIENT_RUNTIME_BINDING_UNSUPPORTED",
        "error_message": f"Explicit external client runtime binding is not supported for subject type: {subject_type}.",
    }


def _commit_manual_rules_apply(
    *,
    job_id: str,
    draft_text: str,
    effective_artifact: dict[str, Any],
    runtime_enforcement: dict[str, Any],
) -> dict[str, Any]:
    return _facade_attr("finalize_manual_rules_apply")(
        job_id=job_id,
        manual_active_text=draft_text,
        effective_artifact=effective_artifact,
        runtime_enforcement=runtime_enforcement,
    )
