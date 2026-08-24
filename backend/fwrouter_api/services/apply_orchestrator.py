from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.custom_servers import VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
from fwrouter_api.services.dataplane_global import read_applied_manifest, read_effective_rules_artifact
from fwrouter_api.services.dataplane_global import validate_global_mode_request
from fwrouter_api.services.dataplane_live import applied_nft_markers_match_live, probe_live_global_mode
from fwrouter_api.services.dataplane_status import build_runtime_enforcement_state, get_dataplane_capability
from fwrouter_api.services.external_vpn import external_vpn_mihomo_reconcile_skip
from fwrouter_api.services.global_mode_profiles import load_precompiled_global_mode_profile, materialize_precompiled_manifest
from fwrouter_api.services.jobs import JobLockConflictError, get_job, touch_job_running
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
from fwrouter_api.services.apply_orchestrator_commits import (
    _clear_subject_user_mode,
    _commit_global_mode,
    _commit_global_server_mode,
    _commit_repaired_global_runtime,
    _commit_selective_default,
    _commit_subject_admin_mode,
    _commit_subject_user_mode,
    _stage_subject_admin_mode,
    _validate_subject_admin_mode,
    _validate_subject_server_override_request,
    _validate_subject_user_mode,
)
from fwrouter_api.services.apply_orchestrator_drift import (
    _applied_manifest_routing_drift,
    _current_routing_drift,
    _live_applied_nft_artifact_consistency,
    _log_artifact_drift,
    _log_routing_drift,
)
from fwrouter_api.services.apply_orchestrator_jobs import _lock_for_intent
from fwrouter_api.services.apply_orchestrator_pipeline import (
    _commit_manual_rules_apply,
    _run_pipeline_for_manifest,
    _run_pipeline_for_state,
    materialize_explicit_external_client_runtime_bindings,
)
from fwrouter_api.services.apply_orchestrator_results import (
    _base_result,
    _build_failure_result,
    _build_success_result,
    _log_mutation_result,
    _scoped_runtime_error_code,
    _scoped_runtime_message,
)
from fwrouter_api.services.apply_orchestrator_state import (
    _find_subject_in_subjects,
    _load_server_override_map,
    _load_subjects_with_overrides,
    _load_user_override_map,
    _persist_global_error,
    _persist_rules_error,
    _persist_subject_failure,
    _routing_mode,
    _subject_follows_global,
    _sync_subject_server_override_statuses,
    _update_subject_apply_state,
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


def _execute_clear_subject_user_mode(job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator_handlers import _execute_clear_subject_user_mode as impl

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


from fwrouter_api.services.apply_orchestrator_public import (  # noqa: E402
    ApplyOrchestrator,
    apply_global_mode_immediately,
    apply_manual_rules,
    clear_subject_user_mode,
    reconcile_current_routing_if_drift,
    repair_global_direct_runtime,
    repair_global_direct_runtime_sync,
    run_apply_mutation,
    set_global_mode,
    set_selective_default,
    set_subject_admin_mode,
    set_subject_mode,
    set_subject_user_mode,
    submit_apply_mutation,
)
