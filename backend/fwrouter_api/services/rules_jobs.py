from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.rules_sources import RulesSourceFetchError, RulesSourcePayload
from fwrouter_api.services import rules as rules_service


def _sanitize_fetch_metadata(fetch_metadata: Any) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    if not isinstance(fetch_metadata, list):
        return sanitized
    for item in fetch_metadata:
        if not isinstance(item, dict):
            continue
        sanitized.append({key: value for key, value in item.items() if key != "raw_text"})
    return sanitized


def _fetch_download_artifacts(
    ruleset_name: str,
    fetch_metadata: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    downloads: dict[str, str] = {}
    metadata_artifacts: dict[str, Any] = {}
    for index, item in enumerate(fetch_metadata, start=1):
        artifact_name = f"{ruleset_name}_{index:02d}"
        downloads[artifact_name] = str(item.get("raw_text") or "")
        metadata_artifacts[artifact_name] = {
            key: value for key, value in item.items() if key != "raw_text"
        }
    return downloads, metadata_artifacts


def _build_fetch_summary(
    info: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fetch_metadata = info.get("fetch_metadata") if isinstance(info.get("fetch_metadata"), list) else []
    commits = sorted(
        {
            str(item.get("commit"))
            for item in fetch_metadata
            if str(item.get("commit") or "").strip()
        }
    )
    version_dates = sorted(
        {
            str(item.get("commit_date") or item.get("last_modified"))
            for item in fetch_metadata
            if str(item.get("commit_date") or item.get("last_modified") or "").strip()
        }
    )
    summary = {
        "configured_urls": list(info.get("source_urls") or []),
        "used_urls": [str(item.get("url")) for item in fetch_metadata if item.get("url")],
        "used_paths": [str(item.get("path")) for item in fetch_metadata if item.get("path")],
        "version_name": info.get("version_name"),
        "git_commits": commits,
        "version_dates": version_dates,
        "latest_upstream_at": version_dates[-1] if version_dates else None,
        "fetches": fetch_metadata,
    }
    if policy is not None:
        summary["source_policy"] = {
            "valid": bool(policy.get("valid")),
            "policy_classification": policy.get("policy_classification"),
            "used_paths": list(policy.get("used_paths") or []),
            "sources": list(policy.get("sources") or []),
            "errors": list(policy.get("errors") or []),
        }
    return summary


def _payload_to_text(payload: RulesSourcePayload | dict[str, Any] | list[str] | None) -> tuple[str, dict[str, Any]]:
    if payload is None:
        return "", {"version_name": None, "source_urls": [], "fetch_metadata": []}
    if isinstance(payload, RulesSourcePayload):
        return "\n".join(payload.values) + ("\n" if payload.values else ""), {
            "version_name": payload.version_name,
            "source_urls": list(payload.source_urls),
            "fetch_metadata": _sanitize_fetch_metadata(payload.fetch_metadata),
        }
    if isinstance(payload, dict):
        values = payload.get("values", [])
        values_list = [str(item) for item in values] if isinstance(values, list) else []
        urls = payload.get("source_urls", [])
        urls_list = [str(item) for item in urls] if isinstance(urls, list) else []
        version_name = payload.get("version_name")
        fetch_metadata = payload.get("fetch_metadata", [])
        return "\n".join(values_list) + ("\n" if values_list else ""), {
            "version_name": str(version_name) if version_name is not None else None,
            "source_urls": urls_list,
            "fetch_metadata": _sanitize_fetch_metadata(fetch_metadata),
        }
    values_list = [str(item) for item in payload] if isinstance(payload, list) else []
    return "\n".join(values_list) + ("\n" if values_list else ""), {
        "version_name": None,
        "source_urls": [],
        "fetch_metadata": [],
    }


def _is_full_update_noop(
    *,
    texts: dict[str, Any],
    direct_info: dict[str, Any],
    vpn_info: dict[str, Any],
    big_direct_text: str,
    big_vpn_text: str,
) -> bool:
    metadata = texts.get("metadata") if isinstance(texts.get("metadata"), dict) else {}
    versions = metadata.get("versions") if isinstance(metadata.get("versions"), dict) else {}
    current_direct = str(texts.get("big_direct_text") or "")
    current_vpn = str(texts.get("big_vpn_text") or "")
    return (
        str(versions.get(rules_service.RULESET_BIG_DIRECT) or "") == str(direct_info.get("version_name") or "")
        and str(versions.get(rules_service.RULESET_BIG_VPN) or "") == str(vpn_info.get("version_name") or "")
        and current_direct == big_direct_text
        and current_vpn == big_vpn_text
    )


def run_rules_full_update(job: dict[str, Any]) -> dict[str, Any]:
    job_id = str(job["job_id"])
    requested_by = str(job.get("requested_by") or "api")
    rules_service.mark_rules_job_running(job_id=job_id, update_type="full_update")

    texts = rules_service.get_manual_rules_texts()
    manual_validation = rules_service.validate_manual_rules(texts["active_text"] or "")
    static_direct_validation = rules_service.validate_value_list(
        texts["static_direct_text"] or "",
        action="DIRECT",
        source=rules_service.RULESET_STATIC_DIRECT,
    )
    if not manual_validation["valid"]:
        failure = {
            "job_status": "failed",
            "error_code": "MANUAL_ACTIVE_INVALID",
            "error_message": "Current manual.active rules are invalid.",
            "rules_state": rules_service.mark_rules_job_failed(
                job_id=job_id,
                code="MANUAL_ACTIVE_INVALID",
                message="Current manual.active rules are invalid.",
                update_type="full_update",
            ),
            "stage": "validate_manual_active",
        }
        rules_service.write_job_json_artifact(job_id, "rules/result.json", failure)
        return failure

    try:
        big_direct_text, direct_info = _payload_to_text(
            rules_service.DEFAULT_RULES_SOURCE_ADAPTER.fetch_big_direct_sources()
        )
        big_vpn_text, vpn_info = _payload_to_text(
            rules_service.DEFAULT_RULES_SOURCE_ADAPTER.fetch_big_vpn_sources()
        )
    except RulesSourceFetchError as exc:
        effective_artifact = rules_service.build_effective_rules_artifact(
            manual_validation=manual_validation,
            selective_default=str(texts["state"]["selective_default"]),
            static_direct_validation=static_direct_validation,
        )
        source_urls = {
            rules_service.RULESET_BIG_DIRECT: list(rules_service._configured_rules_sources().get(rules_service.RULESET_BIG_DIRECT, [])),
            rules_service.RULESET_BIG_VPN: list(rules_service._configured_rules_sources().get(rules_service.RULESET_BIG_VPN, [])),
        }
        failure = {
            "job_status": "failed",
            "error_code": exc.code,
            "error_message": exc.message,
            "stage": "fetch_lists",
            "details": exc.details,
            "rules_state": rules_service.mark_rules_job_failed(
                job_id=job_id,
                code=exc.code,
                message=exc.message,
                update_type="full_update",
                effective_artifact=effective_artifact,
                source_urls=source_urls,
                fetch_summary={
                    rules_service.RULESET_BIG_DIRECT: {
                        "configured_urls": source_urls[rules_service.RULESET_BIG_DIRECT],
                        "used_urls": [],
                        "version_name": None,
                        "fetches": [],
                    },
                    rules_service.RULESET_BIG_VPN: {
                        "configured_urls": source_urls[rules_service.RULESET_BIG_VPN],
                        "used_urls": [],
                        "version_name": None,
                        "fetches": [],
                    },
                },
            ),
        }
        rules_service.write_job_json_artifact(job_id, "rules/result.json", failure)
        rules_service.write_operational_log(
            level="warning",
            event_type="rules_full_update_fetch_failed",
            message="Rules full update failed while downloading external rules sources.",
            details={"job_id": job_id, "code": exc.code, "details": exc.details},
        )
        return failure

    big_direct_validation = rules_service.validate_value_list(
        big_direct_text,
        action="DIRECT",
        source=rules_service.RULESET_BIG_DIRECT,
    )
    big_vpn_policy = rules_service._validate_big_vpn_source_policy(vpn_info)
    big_vpn_validation = rules_service.validate_value_list(
        big_vpn_text,
        action="VPN",
        source=rules_service.RULESET_BIG_VPN,
    )

    direct_fetch_summary = _build_fetch_summary(direct_info)
    vpn_fetch_summary = _build_fetch_summary(vpn_info, policy=big_vpn_policy)
    download_texts = {
        rules_service.RULESET_BIG_DIRECT: big_direct_text,
        rules_service.RULESET_BIG_VPN: big_vpn_text,
    }
    download_metadata: dict[str, Any] = {
        rules_service.RULESET_BIG_DIRECT: direct_fetch_summary,
        rules_service.RULESET_BIG_VPN: vpn_fetch_summary,
    }
    per_source_downloads, per_source_metadata = _fetch_download_artifacts(
        rules_service.RULESET_BIG_DIRECT,
        direct_info.get("fetch_metadata") if isinstance(direct_info, dict) else [],
    )
    download_texts.update(per_source_downloads)
    download_metadata.update(per_source_metadata)
    per_source_downloads, per_source_metadata = _fetch_download_artifacts(
        rules_service.RULESET_BIG_VPN,
        vpn_info.get("fetch_metadata") if isinstance(vpn_info, dict) else [],
    )
    download_texts.update(per_source_downloads)
    download_metadata.update(per_source_metadata)

    if not big_vpn_policy["valid"]:
        effective_artifact = rules_service.build_effective_rules_artifact(
            manual_validation=manual_validation,
            selective_default=str(texts["state"]["selective_default"]),
            static_direct_validation=static_direct_validation,
            big_direct_validation=big_direct_validation,
        )
        failure_message = str(big_vpn_policy["errors"][0]["message"])
        failure = {
            "job_status": "failed",
            "error_code": "RULES_SOURCE_POLICY_VIOLATION",
            "error_message": failure_message,
            "stage": "validate_source_policy",
            "errors": list(big_vpn_policy["errors"]),
            "rules_state": rules_service.mark_rules_job_failed(
                job_id=job_id,
                code="RULES_SOURCE_POLICY_VIOLATION",
                message=failure_message,
                update_type="full_update",
                effective_artifact=effective_artifact,
                source_urls={
                    rules_service.RULESET_BIG_DIRECT: direct_info["source_urls"],
                    rules_service.RULESET_BIG_VPN: vpn_info["source_urls"],
                },
                fetch_summary={
                    rules_service.RULESET_BIG_DIRECT: direct_fetch_summary,
                    rules_service.RULESET_BIG_VPN: vpn_fetch_summary,
                },
            ),
        }
        rules_service.write_rules_candidate(
            job_id=job_id,
            effective_artifact=effective_artifact,
            candidate_text="# invalid rules candidate\n",
            downloads=download_texts,
            download_metadata=download_metadata,
            validations={
                rules_service.RULESET_BIG_DIRECT: big_direct_validation,
                rules_service.RULESET_BIG_VPN: {
                    **big_vpn_validation,
                    "source_policy": big_vpn_policy,
                },
                f"{rules_service.RULESET_BIG_VPN}_source_policy": big_vpn_policy,
            },
        )
        rules_service.write_job_json_artifact(job_id, "rules/result.json", failure)
        rules_service.write_operational_log(
            level="warning",
            event_type="rules_full_update_policy_failed",
            message="Rules full update rejected a big_vpn source that violated source policy.",
            details={"job_id": job_id, "policy": big_vpn_policy},
        )
        return failure

    if not big_direct_validation["valid"] or not big_vpn_validation["valid"]:
        errors = [
            *big_direct_validation["errors"],
            *big_vpn_validation["errors"],
        ]
        failure = {
            "job_status": "failed",
            "error_code": "RULES_VALIDATION_FAILED",
            "error_message": "Large list validation failed.",
            "stage": "validate_lists",
            "errors": errors,
            "rules_state": rules_service.mark_rules_job_failed(
                job_id=job_id,
                code="RULES_VALIDATION_FAILED",
                message="Large list validation failed.",
                update_type="full_update",
                source_urls={
                    rules_service.RULESET_BIG_DIRECT: direct_info["source_urls"],
                    rules_service.RULESET_BIG_VPN: vpn_info["source_urls"],
                },
                fetch_summary={
                    rules_service.RULESET_BIG_DIRECT: direct_fetch_summary,
                    rules_service.RULESET_BIG_VPN: vpn_fetch_summary,
                },
            ),
        }
        rules_service.write_rules_candidate(
            job_id=job_id,
            effective_artifact=rules_service.build_effective_rules_artifact(
                manual_validation=manual_validation,
                selective_default=str(texts["state"]["selective_default"]),
                static_direct_validation=static_direct_validation,
            ),
            candidate_text="# invalid rules candidate\n",
            downloads=download_texts,
            download_metadata=download_metadata,
            validations={
                rules_service.RULESET_BIG_DIRECT: big_direct_validation,
                rules_service.RULESET_BIG_VPN: big_vpn_validation,
            },
        )
        rules_service.write_job_json_artifact(job_id, "rules/result.json", failure)
        return failure

    effective_artifact = rules_service.build_effective_rules_artifact(
        manual_validation=manual_validation,
        selective_default=str(texts["state"]["selective_default"]),
        static_direct_validation=static_direct_validation,
        big_direct_validation=big_direct_validation,
        big_vpn_validation=big_vpn_validation,
    )
    candidate_text = rules_service.render_effective_rules_text(effective_artifact)
    rules_service.write_rules_candidate(
        job_id=job_id,
        effective_artifact=effective_artifact,
        candidate_text=candidate_text,
        downloads=download_texts,
        download_metadata=download_metadata,
        validations={
            rules_service.RULESET_MANUAL: manual_validation,
            rules_service.RULESET_STATIC_DIRECT: static_direct_validation,
            rules_service.RULESET_BIG_DIRECT: big_direct_validation,
            rules_service.RULESET_BIG_VPN: big_vpn_validation,
        },
    )

    if _is_full_update_noop(
        texts=texts,
        direct_info=direct_info,
        vpn_info=vpn_info,
        big_direct_text=big_direct_validation["normalized_text"],
        big_vpn_text=big_vpn_validation["normalized_text"],
    ):
        state = rules_service.mark_rules_job_success(job_id=job_id, update_type="full_update")
        result = {
            "job_status": "success",
            "job_id": job_id,
            "status": "success",
            "stage": "noop",
            "changed": False,
            "rules_state": state,
            "versions": {
                rules_service.RULESET_BIG_DIRECT: direct_info["version_name"],
                rules_service.RULESET_BIG_VPN: vpn_info["version_name"],
            },
            "message": "Rules sources are already at the latest applied version.",
        }
        rules_service.write_job_json_artifact(job_id, "rules/result.json", result)
        rules_service.write_operational_log(
            event_type="rules_full_update_noop",
            message="Rules full update detected no upstream changes; active artifacts were preserved.",
            details={"job_id": job_id},
        )
        return result

    apply_result = rules_service.run_apply_pipeline(
        job_id=job_id,
        reason=rules_service.JOB_TYPE_RULES_FULL_UPDATE,
        mode=rules_service.ApplyMode.APPLY,
        input_data={"intent": rules_service.JOB_TYPE_RULES_FULL_UPDATE, "requested_by": requested_by},
    )
    if not apply_result["ok"]:
        result = {
            "job_status": "failed",
            "error_code": apply_result["dataplane"]["error_code"] or "RULES_APPLY_FAILED",
            "error_message": apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
            "stage": apply_result["stage"],
            "apply": apply_result,
            "rules_state": rules_service.mark_rules_job_failed(
                job_id=job_id,
                code=apply_result["dataplane"]["error_code"] or "RULES_APPLY_FAILED",
                message=apply_result["dataplane"]["error_message"] or apply_result["dataplane"]["message"],
                update_type="full_update",
                effective_artifact=effective_artifact,
                source_urls={
                    rules_service.RULESET_BIG_DIRECT: direct_info["source_urls"],
                    rules_service.RULESET_BIG_VPN: vpn_info["source_urls"],
                },
                fetch_summary={
                    rules_service.RULESET_BIG_DIRECT: direct_fetch_summary,
                    rules_service.RULESET_BIG_VPN: vpn_fetch_summary,
                },
            ),
        }
        rules_service.write_job_json_artifact(job_id, "rules/result.json", result)
        rules_service.write_operational_log(
            level="warning",
            event_type="rules_full_update_failed",
            message="Rules full update failed; active/effective rules were preserved.",
            details={"job_id": job_id, "stage": apply_result["stage"]},
        )
        return result

    applied_runtime_enforcement = {
        "dataplane_capability": apply_result["dataplane_capability"],
        "capability": apply_result["dataplane_capability"],
        "enforcement_level": apply_result["enforcement_level"],
        "traffic_enforcement_guaranteed": apply_result["traffic_enforcement_guaranteed"],
        "supported_modes": dict(apply_result.get("supported_modes") or {}),
        "missing_runtime_requirements": list(apply_result.get("missing_runtime_requirements") or []),
    }
    effective_artifact["runtime_enforcement"] = applied_runtime_enforcement

    metadata = rules_service._build_metadata_file(
        job_id=job_id,
        status="success",
        selective_default=str(effective_artifact["selective_default"]),
        source_counts=dict(effective_artifact["source_counts"]),
        effective_counts=dict(effective_artifact["effective_counts"]),
        versions={
            rules_service.RULESET_BIG_DIRECT: direct_info["version_name"],
            rules_service.RULESET_BIG_VPN: vpn_info["version_name"],
        },
        source_urls={
            rules_service.RULESET_BIG_DIRECT: direct_info["source_urls"],
            rules_service.RULESET_BIG_VPN: vpn_info["source_urls"],
        },
        fetch_summary={
            rules_service.RULESET_BIG_DIRECT: direct_fetch_summary,
            rules_service.RULESET_BIG_VPN: vpn_fetch_summary,
        },
    )
    promoted = rules_service.write_active_rules_state(
        manual_active_text=None,
        big_direct_text=big_direct_validation["normalized_text"],
        big_vpn_text=big_vpn_validation["normalized_text"],
        effective_artifact=effective_artifact,
        metadata=metadata,
    )
    from fwrouter_api.services.dnsmasq import reconcile_dnsmasq_rules

    dnsmasq_reconcile = reconcile_dnsmasq_rules()
    if not dnsmasq_reconcile.get("ok"):
        rules_service.write_operational_log(
            level="warning",
            event_type="rules_full_update_dnsmasq_failed",
            message="Dnsmasq rules reconcile failed after rules update.",
            details={"job_id": job_id, "dnsmasq": dnsmasq_reconcile},
        )

    mihomo_reconcile = rules_service.reconcile_mihomo_runtime()
    if not mihomo_reconcile["ok"]:
        result = {
            "job_status": "failed",
            "error_code": "MIHOMO_RECONCILE_FAILED",
            "error_message": "Failed to reconcile Mihomo runtime after rules update.",
            "stage": str(mihomo_reconcile.get("stage") or "mihomo_reconcile"),
            "mihomo": mihomo_reconcile,
            "rules_state": rules_service.mark_rules_job_failed(
                job_id=job_id,
                code="MIHOMO_RECONCILE_FAILED",
                message="Failed to reconcile Mihomo runtime after rules update.",
                update_type="full_update",
                effective_artifact=effective_artifact,
                source_urls={
                    rules_service.RULESET_BIG_DIRECT: direct_info["source_urls"],
                    rules_service.RULESET_BIG_VPN: vpn_info["source_urls"],
                },
                fetch_summary={
                    rules_service.RULESET_BIG_DIRECT: direct_fetch_summary,
                    rules_service.RULESET_BIG_VPN: vpn_fetch_summary,
                },
            ),
        }
        rules_service.write_job_json_artifact(job_id, "rules/result.json", result)
        return result

    state = rules_service.mark_rules_job_success(job_id=job_id, update_type="full_update")
    rules_service.update_rules_metadata_records(
        job_id=job_id,
        effective_artifact=effective_artifact,
        big_direct_version=direct_info["version_name"],
        big_vpn_version=vpn_info["version_name"],
        source_urls={
            rules_service.RULESET_BIG_DIRECT: direct_info["source_urls"],
            rules_service.RULESET_BIG_VPN: vpn_info["source_urls"],
        },
        fetch_summary={
            rules_service.RULESET_BIG_DIRECT: direct_fetch_summary,
            rules_service.RULESET_BIG_VPN: vpn_fetch_summary,
        },
        status="active",
    )

    result = {
        "job_status": "success",
        "job_id": job_id,
        "status": "success",
        "stage": "commit",
        "changed": True,
        "apply": apply_result,
        "mihomo": mihomo_reconcile,
        "rules_state": state,
        "promoted_paths": promoted,
        "dataplane_capability": apply_result["dataplane_capability"],
        "traffic_enforcement_guaranteed": apply_result["traffic_enforcement_guaranteed"],
    }
    rules_service.write_job_json_artifact(job_id, "rules/result.json", result)
    rules_service.write_operational_log(
        event_type="rules_full_update_succeeded",
        message="Rules full update completed and promoted bounded active artifacts.",
        details={"job_id": job_id},
    )
    return result


def submit_rules_full_update(
    *,
    requested_by: str = "api",
    run_now: bool = True,
) -> dict[str, Any]:
    from fwrouter_api.jobs.extended_handlers import register_extended_handlers

    manager = rules_service.get_default_job_manager()
    register_extended_handlers(manager)
    job = manager.create(
        rules_service.JOB_TYPE_RULES_FULL_UPDATE,
        lock_key=rules_service.LOCK_RULES_APPLY,
        requested_by=requested_by,
        input_data={"requested_by": requested_by},
    )
    if run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job
    return job


def apply_manual_rules(*, requested_by: str = "api") -> dict[str, Any]:
    from fwrouter_api.services.apply_orchestrator import apply_manual_rules as run_manual_rules_transaction

    return run_manual_rules_transaction(requested_by=requested_by)
