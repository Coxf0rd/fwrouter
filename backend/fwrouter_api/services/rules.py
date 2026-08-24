from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.rules_sources import (
    DEFAULT_RULES_SOURCE_ADAPTER,
    RulesSourceFetchError,
    RulesSourcePayload,
)
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.apply import ApplyMode, run_apply_pipeline
from fwrouter_api.services.artifacts import (
    atomic_write_json,
    atomic_write_text,
    write_job_json_artifact,
    write_job_text_artifact,
)
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime


from fwrouter_api.services.rules_compile import (
    BIG_VPN_BROAD_AGGREGATE_PATHS,
    DOMAIN_LABEL_RE,
    DOMAIN_RE,
    JOB_TYPE_RULES_FULL_UPDATE,
    LOCK_RULES_APPLY,
    PROTECTED_LOCAL_NETWORKS,
    PROTECTED_SERVICE_DOMAINS,
    RULESET_BIG_DIRECT,
    RULESET_BIG_VPN,
    RULESET_EFFECTIVE,
    RULESET_MANUAL,
    RULESET_ORDER,
    RULESET_STATIC_DIRECT,
    RULES_PIPELINE_VERSION,
    RULE_ACTION_ALIASES,
    RULE_ACTIONS,
    _build_rule_entry,
    _classify_big_vpn_source,
    _collapse_rule_networks,
    _compile_large_list_rules,
    _configured_rules_sources,
    _extract_explicit_source_paths,
    _is_protected_local,
    _network_for_rule,
    _normalize_domain_name,
    _normalize_large_list_value,
    _normalize_rule_value,
    _protected_rules,
    _rules_from_validation,
    _suffix_subsumed,
    _utc_now_iso,
    _validate_big_vpn_source_policy,
    build_effective_rules_artifact,
    render_effective_rules_text,
    validate_manual_rules,
    validate_value_list,
)

def _default_rules_paths() -> dict[str, Path]:
    from fwrouter_api.services.rules_state import _default_rules_paths as impl

    return impl()


def _normalize_path(value: str | None, fallback: Path) -> Path:
    from fwrouter_api.services.rules_state import _normalize_path as impl

    return impl(value, fallback)


def _read_text_if_exists(path: Path | None) -> str | None:
    from fwrouter_api.services.rules_state import _read_text_if_exists as impl

    return impl(path)


def _read_json_if_exists(path: Path | None) -> dict[str, Any] | None:
    from fwrouter_api.services.rules_state import _read_json_if_exists as impl

    return impl(path)


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    from fwrouter_api.services.rules_state import _json_dumps as impl

    return impl(value)


def _json_loads(value: str | None) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _json_loads as impl

    return impl(value)


def _default_rules_state() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _default_rules_state as impl

    return impl()


def _row_to_rules_state(row: Any | None) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _row_to_rules_state as impl

    return impl(row)


def get_rules_state() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_rules_state as impl

    return impl()


def _upsert_rules_state_record(state: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _upsert_rules_state_record as impl

    return impl(state)


def _rules_state_with_updates(**updates: Any) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _rules_state_with_updates as impl

    return impl(**updates)


def effective_rules_with_selective_default(
    effective_artifact: dict[str, Any] | None,
    *,
    selective_default: str,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import effective_rules_with_selective_default as impl

    return impl(effective_artifact, selective_default=selective_default)


def sync_active_selective_default(
    *,
    selective_default: str,
    job_id: str | None = None,
    effective_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import sync_active_selective_default as impl

    return impl(
        selective_default=selective_default,
        job_id=job_id,
        effective_artifact=effective_artifact,
    )


def list_rules_metadata() -> list[dict[str, Any]]:
    from fwrouter_api.services.rules_state import list_rules_metadata as impl

    return impl()


def _ensure_seed_files(paths: dict[str, Path]) -> None:
    from fwrouter_api.services.rules_state import _ensure_seed_files as impl

    return impl(paths)


def get_manual_rules_texts() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_manual_rules_texts as impl

    return impl()


def _build_metadata_file(
    *,
    job_id: str,
    status: str,
    selective_default: str,
    source_counts: dict[str, Any],
    effective_counts: dict[str, Any],
    versions: dict[str, Any] | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import _build_metadata_file as impl

    return impl(
        job_id=job_id,
        status=status,
        selective_default=selective_default,
        source_counts=source_counts,
        effective_counts=effective_counts,
        versions=versions,
        source_urls=source_urls,
        fetch_summary=fetch_summary,
        error_code=error_code,
        error_message=error_message,
    )


def _mirror_file(source: Path, destination: Path) -> None:
    from fwrouter_api.services.rules_state import _mirror_file as impl

    return impl(source, destination)


def _snapshot_last_good_rules(paths: dict[str, Any]) -> None:
    from fwrouter_api.services.rules_state import _snapshot_last_good_rules as impl

    return impl(paths)


def restore_last_good_rules() -> dict[str, str]:
    from fwrouter_api.services.rules_state import restore_last_good_rules as impl

    return impl()


def write_rules_candidate(
    *,
    job_id: str,
    effective_artifact: dict[str, Any],
    candidate_text: str,
    downloads: dict[str, str] | None = None,
    download_metadata: dict[str, Any] | None = None,
    validations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    from fwrouter_api.services.rules_state import write_rules_candidate as impl

    return impl(
        job_id=job_id,
        effective_artifact=effective_artifact,
        candidate_text=candidate_text,
        downloads=downloads,
        download_metadata=download_metadata,
        validations=validations,
    )


def write_active_rules_state(
    *,
    manual_active_text: str | None,
    big_direct_text: str | None,
    big_vpn_text: str | None,
    effective_artifact: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import write_active_rules_state as impl

    return impl(
        manual_active_text=manual_active_text,
        big_direct_text=big_direct_text,
        big_vpn_text=big_vpn_text,
        effective_artifact=effective_artifact,
        metadata=metadata,
    )


def _upsert_ruleset_metadata(
    *,
    ruleset_type: str,
    active_path: str,
    status: str,
    job_id: str,
    metadata: dict[str, Any],
    version_name: str | None = None,
    source_urls: list[str] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    from fwrouter_api.services.rules_state import _upsert_ruleset_metadata as impl

    return impl(
        ruleset_type=ruleset_type,
        active_path=active_path,
        status=status,
        job_id=job_id,
        metadata=metadata,
        version_name=version_name,
        source_urls=source_urls,
        error_code=error_code,
        error_message=error_message,
    )


def update_rules_metadata_records(
    *,
    job_id: str,
    effective_artifact: dict[str, Any],
    big_direct_version: str | None = None,
    big_vpn_version: str | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
    status: str = "active",
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    from fwrouter_api.services.rules_state import update_rules_metadata_records as impl

    return impl(
        job_id=job_id,
        effective_artifact=effective_artifact,
        big_direct_version=big_direct_version,
        big_vpn_version=big_vpn_version,
        source_urls=source_urls,
        fetch_summary=fetch_summary,
        status=status,
        error_code=error_code,
        error_message=error_message,
    )


def mark_rules_job_running(*, job_id: str, update_type: str) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import mark_rules_job_running as impl

    return impl(job_id=job_id, update_type=update_type)


def mark_rules_job_failed(
    *,
    job_id: str,
    code: str,
    message: str,
    update_type: str,
    effective_artifact: dict[str, Any] | None = None,
    source_urls: dict[str, list[str]] | None = None,
    fetch_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import mark_rules_job_failed as impl

    return impl(
        job_id=job_id,
        code=code,
        message=message,
        update_type=update_type,
        effective_artifact=effective_artifact,
        source_urls=source_urls,
        fetch_summary=fetch_summary,
    )


def mark_rules_job_success(
    *,
    job_id: str,
    update_type: str,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import mark_rules_job_success as impl

    return impl(job_id=job_id, update_type=update_type)


def get_rules_overview() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_rules_overview as impl

    return impl()


def get_rules_summary() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_rules_summary as impl

    return impl()


def save_manual_draft(text: str) -> dict[str, Any]:
    from fwrouter_api.services.rules_state import save_manual_draft as impl

    return impl(text)


def get_effective_rules() -> dict[str, Any]:
    from fwrouter_api.services.rules_state import get_effective_rules as impl

    return impl()


def prepare_manual_rules_candidate(*, job_id: str) -> dict[str, Any]:
    from fwrouter_api.services.rules_artifacts import prepare_manual_rules_candidate as impl

    return impl(job_id=job_id)


def finalize_manual_rules_apply(
    *,
    job_id: str,
    manual_active_text: str,
    effective_artifact: dict[str, Any],
    runtime_enforcement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_artifacts import finalize_manual_rules_apply as impl

    return impl(
        job_id=job_id,
        manual_active_text=manual_active_text,
        effective_artifact=effective_artifact,
        runtime_enforcement=runtime_enforcement,
    )


def _sanitize_fetch_metadata(fetch_metadata: Any) -> list[dict[str, Any]]:
    from fwrouter_api.services.rules_jobs import _sanitize_fetch_metadata as impl

    return impl(fetch_metadata)


def _fetch_download_artifacts(
    ruleset_name: str,
    fetch_metadata: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    from fwrouter_api.services.rules_jobs import _fetch_download_artifacts as impl

    return impl(ruleset_name, fetch_metadata)


def _build_fetch_summary(
    info: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_jobs import _build_fetch_summary as impl

    return impl(info, policy=policy)


def _payload_to_text(payload: RulesSourcePayload | dict[str, Any] | list[str] | None) -> tuple[str, dict[str, Any]]:
    from fwrouter_api.services.rules_jobs import _payload_to_text as impl

    return impl(payload)


def _is_full_update_noop(
    *,
    texts: dict[str, Any],
    direct_info: dict[str, Any],
    vpn_info: dict[str, Any],
    big_direct_text: str,
    big_vpn_text: str,
) -> bool:
    from fwrouter_api.services.rules_jobs import _is_full_update_noop as impl

    return impl(
        texts=texts,
        direct_info=direct_info,
        vpn_info=vpn_info,
        big_direct_text=big_direct_text,
        big_vpn_text=big_vpn_text,
    )


def run_rules_full_update(job: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services.rules_jobs import run_rules_full_update as impl

    return impl(job)


def submit_rules_full_update(
    *,
    requested_by: str = "api",
    run_now: bool = True,
) -> dict[str, Any]:
    from fwrouter_api.services.rules_jobs import submit_rules_full_update as impl

    return impl(requested_by=requested_by, run_now=run_now)


def apply_manual_rules(*, requested_by: str = "api") -> dict[str, Any]:
    from fwrouter_api.services.rules_jobs import apply_manual_rules as impl

    return impl(requested_by=requested_by)
