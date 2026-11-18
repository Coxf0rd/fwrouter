from __future__ import annotations

from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.subject_policy import get_routing_snapshot
from fwrouter_api.services.apply_orchestrator_constants import (
    GLOBAL_ARTIFACT_DRIFT_CODE,
    GLOBAL_ROUTING_DRIFT_CODE,
    LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE,
)

from fwrouter_api.services.dataplane_global import (
    build_global_preflight,
    read_applied_manifest as read_applied_manifest,
)
from fwrouter_api.services.dataplane_live import (
    applied_nft_markers_match_live as applied_nft_markers_match_live,
    probe_live_global_mode as probe_live_global_mode,
)
from fwrouter_api.services.logs import (
    write_operational_log as write_operational_log,
    write_technical_log as write_technical_log,
)


def _current_routing_drift(
    *,
    routing: dict[str, Any] | None = None,
    check_policy_readiness: bool = False,
) -> dict[str, Any]:
    """Inspect persisted/live drift; fresh policy readiness is opt-in for convergence."""
    resolved_routing = dict(routing or get_routing_snapshot() or {})
    live_probe = probe_live_global_mode()
    live_artifact_consistency = _probe_live_artifact_consistency()
    expected_mode = str(
        resolved_routing.get("applied_mode") or resolved_routing.get("desired_mode") or "direct"
    ).strip().lower()
    from fwrouter_api.services.provider_recovery import emergency_override
    if emergency_override():
        expected_mode = "direct"
    expected_selective_default = str(
        resolved_routing.get("selective_default") or "direct"
    ).strip().lower()
    live_mode = str(live_probe.get("mode") or "unknown").strip().lower()
    live_selective_default = str(
        live_probe.get("selective_default") or "direct"
    ).strip().lower()
    matches_intent = bool(
        live_probe.get("ok")
        and live_mode == expected_mode
        and (
            expected_mode != "selective"
            or live_selective_default == expected_selective_default
        )
    )
    live_artifact_matches = not bool(live_artifact_consistency.get("detected"))
    # A selective ruleset can remain byte-for-byte installed while its VPN
    # health contract changes. The installed artifact still matches itself,
    # but its degraded DROP/JUMP decision is stale. Compare the applied
    # preflight receipt with current readiness and route changes through the
    # normal guarded apply owner.
    policy_readiness_drift: dict[str, Any] = {
        "detected": False, "checked": False, "state": "not_applicable",
    }
    applied_manifest = read_applied_manifest()
    applied_preflight = (
        applied_manifest.get("global_preflight")
        if isinstance(applied_manifest, dict)
        else None
    )
    applied_rules = (
        applied_preflight.get("selective_rules")
        if isinstance(applied_preflight, dict)
        else None
    )
    applied_subjects = (
        applied_manifest.get("subjects") if isinstance(applied_manifest, dict) else None
    )
    scoped_selective_subjects = False
    if isinstance(applied_subjects, list):
        for subject in applied_subjects:
            if not isinstance(subject, dict) or not bool(subject.get("is_active")):
                continue
            effective_state = subject.get("effective_state")
            effective_mode = (
                effective_state.get("effective_mode")
                if isinstance(effective_state, dict)
                else ""
            )
            path = str(
                subject.get("dataplane_path")
                or (
                    effective_state.get("dataplane_path")
                    if isinstance(effective_state, dict)
                    else ""
                )
                or (effective_mode if effective_mode in {"direct", "vpn", "selective"} else "")
                or ""
            ).strip().lower()
            if path == "selective":
                scoped_selective_subjects = True
                break
    if check_policy_readiness and (
        expected_mode == "selective" or scoped_selective_subjects
    ):
        current_preflight = build_global_preflight(routing=resolved_routing)
        current_rules = current_preflight.get("selective_rules")
        vpn_policy_required = (
            (
                isinstance(applied_rules, dict)
                and applied_rules.get("requires_vpn_runtime") is True
            )
            or (
                isinstance(current_rules, dict)
                and current_rules.get("requires_vpn_runtime") is True
            )
        )
        if vpn_policy_required:
            applied_ready = (
                applied_preflight.get("selective_vpn_policy_ready")
                if isinstance(applied_preflight, dict) else None
            )
            current_ready = current_preflight.get("selective_vpn_policy_ready")
            readiness_valid = isinstance(applied_ready, bool) and isinstance(current_ready, bool)
            policy_readiness_drift = {
                # Refresh an incomplete receipt through the guarded apply path.
                # Rendering remains fail-closed when current readiness is false.
                "detected": (applied_ready != current_ready) if readiness_valid else True,
                "checked": readiness_valid,
                "state": "compared" if readiness_valid else "unknown",
                "applied_ready": applied_ready if readiness_valid else None,
                "current_ready": current_ready if isinstance(current_ready, bool) else None,
                "applied_degraded": (
                    applied_preflight.get("selective_degraded")
                    if isinstance(applied_preflight, dict) else None
                ),
                "current_degraded": current_preflight.get("selective_degraded"),
            }
    drift_detected = (
        not matches_intent
        or not live_artifact_matches
        or bool(policy_readiness_drift.get("detected"))
    )
    drift_code = None
    if not matches_intent:
        drift_code = GLOBAL_ROUTING_DRIFT_CODE
    elif not live_artifact_matches:
        drift_code = LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE
    elif policy_readiness_drift.get("detected"):
        drift_code = "SELECTIVE_VPN_POLICY_READINESS_DRIFT"
    return {
        "detected": drift_detected,
        "code": drift_code,
        "routing": resolved_routing,
        "expected_mode": expected_mode,
        "expected_selective_default": expected_selective_default,
        "live_probe": live_probe,
        "live_mode": live_mode,
        "live_selective_default": live_selective_default,
        "live_artifact_consistency": live_artifact_consistency,
        "policy_readiness_drift": policy_readiness_drift,
    }


def _live_applied_nft_artifact_consistency() -> dict[str, Any]:
    settings = get_settings()
    dataplane_dir = settings.paths.generated_dir / "dataplane"
    applied_manifest_path = dataplane_dir / "applied-manifest.json"
    applied_nft_path = dataplane_dir / "applied.nft"
    if not applied_manifest_path.exists() or not applied_nft_path.exists():
        return {
            "detected": False,
            "checked": False,
            "reason": "applied_artifacts_missing",
            "applied_manifest_path": str(applied_manifest_path),
            "applied_nft_path": str(applied_nft_path),
        }

    consistency = applied_nft_markers_match_live(applied_nft_path)
    return {
        "detected": not bool(consistency.get("ok", True)),
        "checked": True,
        "applied_manifest_path": str(applied_manifest_path),
        "applied_nft_path": str(applied_nft_path),
        **consistency,
    }


_probe_live_artifact_consistency = _live_applied_nft_artifact_consistency



def _applied_manifest_routing_drift(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_routing = dict(routing or get_routing_snapshot() or {})
    applied_manifest = read_applied_manifest()
    if not (
        isinstance(applied_manifest, dict)
        and isinstance(applied_manifest.get("routing_global_state"), dict)
    ):
        return {
            "detected": False,
            "code": None,
            "routing": resolved_routing,
            "applied_manifest_routing": None,
            "mismatches": {},
        }

    manifest_routing = dict(applied_manifest.get("routing_global_state"))
    from fwrouter_api.services.provider_recovery import override_manifest_state
    resolved_routing, _ = override_manifest_state(resolved_routing, [])

    routing_keys = (
        "desired_mode",
        "applied_mode",
        "selective_default",
        "server_mode",
        "desired_fixed_server_id",
        "applied_fixed_server_id",
    )
    mismatches = {
        key: {
            "routing": resolved_routing.get(key),
            "applied_manifest": manifest_routing.get(key),
        }
        for key in routing_keys
        if resolved_routing.get(key) != manifest_routing.get(key)
    }

    return {
        "detected": bool(mismatches),
        "code": GLOBAL_ARTIFACT_DRIFT_CODE if mismatches else None,
        "routing": resolved_routing,
        "applied_manifest_routing": manifest_routing,
        "mismatches": mismatches,
    }


def _log_routing_drift(*, intent: str, requested_by: str, drift: dict[str, Any]) -> None:
    if not drift.get("detected"):
        return
    details = {
        "intent": intent,
        "requested_by": requested_by,
        "code": drift.get("code") or GLOBAL_ROUTING_DRIFT_CODE,
        "routing": drift.get("routing"),
        "expected_mode": drift.get("expected_mode"),
        "expected_selective_default": drift.get("expected_selective_default"),
        "live_mode": drift.get("live_mode"),
        "live_selective_default": drift.get("live_selective_default"),
        "live_probe": drift.get("live_probe"),
        "live_artifact_consistency": drift.get("live_artifact_consistency"),
    }
    write_operational_log(
        event_type="routing_live_drift_detected",
        level="warning",
        message="Persisted global routing state does not match live dataplane mode.",
        details=details,
    )
    write_technical_log(
        component="apply-orchestrator",
        level="warning",
        event_type="routing_live_drift_detected",
        message="Persisted global routing state does not match live dataplane mode.",
        details=details,
    )


def _log_artifact_drift(*, intent: str, requested_by: str, drift: dict[str, Any]) -> None:
    if not drift.get("detected"):
        return
    details = {
        "intent": intent,
        "requested_by": requested_by,
        "code": drift.get("code") or GLOBAL_ARTIFACT_DRIFT_CODE,
        "routing": drift.get("routing"),
        "applied_manifest_routing": drift.get("applied_manifest_routing"),
        "mismatches": drift.get("mismatches"),
    }
    write_operational_log(
        event_type="routing_artifact_drift_detected",
        level="warning",
        message="Applied routing manifest does not match persisted global routing state.",
        details=details,
    )
    write_technical_log(
        component="apply-orchestrator",
        level="warning",
        event_type="routing_artifact_drift_detected",
        message="Applied routing manifest does not match persisted global routing state.",
        details=details,
    )
