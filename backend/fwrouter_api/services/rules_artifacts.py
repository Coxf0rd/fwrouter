from __future__ import annotations

from typing import Any

from fwrouter_api.services import rules as rules_service


def prepare_manual_rules_candidate(*, job_id: str) -> dict[str, Any]:
    texts = rules_service.get_manual_rules_texts()
    manual_validation = rules_service.validate_manual_rules(texts["draft_text"] or "")
    static_direct_validation = rules_service.validate_value_list(
        texts["static_direct_text"] or "",
        action="DIRECT",
        source=rules_service.RULESET_STATIC_DIRECT,
    )
    big_direct_validation = rules_service.validate_value_list(
        texts["big_direct_text"] or "",
        action="DIRECT",
        source=rules_service.RULESET_BIG_DIRECT,
    )
    big_vpn_validation = rules_service.validate_value_list(
        texts["big_vpn_text"] or "",
        action="VPN",
        source=rules_service.RULESET_BIG_VPN,
    )

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
        validations={
            rules_service.RULESET_MANUAL: manual_validation,
            rules_service.RULESET_STATIC_DIRECT: static_direct_validation,
            rules_service.RULESET_BIG_DIRECT: big_direct_validation,
            rules_service.RULESET_BIG_VPN: big_vpn_validation,
        },
    )
    return {
        "manual_validation": manual_validation,
        "static_direct_validation": static_direct_validation,
        "big_direct_validation": big_direct_validation,
        "big_vpn_validation": big_vpn_validation,
        "effective_artifact": effective_artifact,
        "candidate_text": candidate_text,
    }


def finalize_manual_rules_apply(
    *,
    job_id: str,
    manual_active_text: str,
    effective_artifact: dict[str, Any],
    runtime_enforcement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_to_write = dict(effective_artifact)
    effective_to_write["runtime_enforcement"] = dict(
        runtime_enforcement
        or effective_artifact.get("runtime_enforcement")
        or rules_service.build_runtime_enforcement_state()
    )
    metadata = rules_service._build_metadata_file(
        job_id=job_id,
        status="success",
        selective_default=str(effective_to_write["selective_default"]),
        source_counts=dict(effective_to_write["source_counts"]),
        effective_counts=dict(effective_to_write["effective_counts"]),
    )
    paths = rules_service.write_active_rules_state(
        manual_active_text=manual_active_text,
        big_direct_text=None,
        big_vpn_text=None,
        effective_artifact=effective_to_write,
        metadata=metadata,
    )

    from fwrouter_api.services.dnsmasq import reconcile_dnsmasq_rules

    dnsmasq_reconcile = reconcile_dnsmasq_rules()
    if not dnsmasq_reconcile.get("ok"):
        rules_service.write_operational_log(
            level="warning",
            event_type="rules_manual_update_dnsmasq_failed",
            message="Dnsmasq rules reconcile failed after manual rules update.",
            details={"job_id": job_id, "dnsmasq": dnsmasq_reconcile},
        )

    state = rules_service.mark_rules_job_success(job_id=job_id, update_type="manual_apply")
    rules_service.update_rules_metadata_records(
        job_id=job_id,
        effective_artifact=effective_artifact,
        status="active",
    )
    return {
        "state": state,
        "paths": paths,
        "active_text": manual_active_text,
        "effective_counts": dict(effective_to_write.get("effective_counts") or {}),
        "source_counts": dict(effective_to_write.get("source_counts") or {}),
    }
