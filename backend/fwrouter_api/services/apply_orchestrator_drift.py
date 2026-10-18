from __future__ import annotations

from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.subject_policy import get_routing_snapshot
from fwrouter_api.services.apply_orchestrator_constants import (
    GLOBAL_ARTIFACT_DRIFT_CODE,
    GLOBAL_ROUTING_DRIFT_CODE,
    LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE,
)

def _facade_attr(name: str) -> Any:
    from fwrouter_api.services import apply_orchestrator as orchestrator

    return getattr(orchestrator, name)


def _current_routing_drift(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_routing = dict(routing or get_routing_snapshot() or {})
    live_probe = _facade_attr("probe_live_global_mode")()
    live_artifact_consistency = _facade_attr("_live_applied_nft_artifact_consistency")()
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
    drift_detected = not matches_intent or not live_artifact_matches
    drift_code = None
    if not matches_intent:
        drift_code = GLOBAL_ROUTING_DRIFT_CODE
    elif not live_artifact_matches:
        drift_code = LIVE_DATAPLANE_ARTIFACT_DRIFT_CODE
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

    consistency = _facade_attr("applied_nft_markers_match_live")(applied_nft_path)
    return {
        "detected": not bool(consistency.get("ok", True)),
        "checked": True,
        "applied_manifest_path": str(applied_manifest_path),
        "applied_nft_path": str(applied_nft_path),
        **consistency,
    }


def _applied_manifest_routing_drift(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_routing = dict(routing or get_routing_snapshot() or {})
    applied_manifest = _facade_attr("read_applied_manifest")()
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
    _facade_attr("write_operational_log")(
        event_type="routing_live_drift_detected",
        level="warning",
        message="Persisted global routing state does not match live dataplane mode.",
        details=details,
    )
    _facade_attr("write_technical_log")(
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
    _facade_attr("write_operational_log")(
        event_type="routing_artifact_drift_detected",
        level="warning",
        message="Applied routing manifest does not match persisted global routing state.",
        details=details,
    )
    _facade_attr("write_technical_log")(
        component="apply-orchestrator",
        level="warning",
        event_type="routing_artifact_drift_detected",
        message="Applied routing manifest does not match persisted global routing state.",
        details=details,
    )
