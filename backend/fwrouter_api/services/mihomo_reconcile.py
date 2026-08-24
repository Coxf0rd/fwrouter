from __future__ import annotations

import filecmp
import os
import shutil
from pathlib import Path
from typing import Any

from fwrouter_api.services import mihomo_config as config
from fwrouter_api.services.artifacts import atomic_write_text
from fwrouter_api.services.mihomo_reconcile_fingerprint import (
    current_mihomo_input_fingerprint,
    mihomo_input_unchanged,
    write_mihomo_reconcile_fingerprint_state,
)


def reconcile_mihomo_selective_default_fast(
    routing: dict[str, Any] | None = None,
    job_id: str = "manual",
) -> dict[str, Any]:
    """Patch only FWRouter transparent fallback when selective_default changed.

    The full Mihomo reconcile rebuilds and validates a very large rules YAML.
    For selective_default toggles the rule inventory is unchanged; only the
    final fallback of the FWRouter-owned transparent subrule and FWRouter
    metadata need to change. If the active config does not match this narrow
    shape, callers must fall back to the full reconcile path.
    """

    blocked = config.managed_runtime_operation_blocked(
        "vpn",
        error_code="MIHOMO_MANAGED_RUNTIME_REQUIRED",
        operation="mihomo_selective_default_fast_reconcile",
    )
    if blocked is not None:
        return {
            **blocked,
            "job_id": job_id,
            "reconcile_action": "none",
            "reconcile_reason": "managed_runtime_required",
            "fast_path": True,
        }

    routing_dict = routing if isinstance(routing, dict) else {}
    target_default = config._resolved_selective_default(routing_dict)
    if target_default not in {"direct", "vpn"}:
        return {
            "ok": False,
            "job_id": job_id,
            "reconcile_action": "none",
            "reconcile_reason": "invalid_selective_default",
            "fast_path": True,
        }

    base_path = Path(config._resolved_base_config_path())
    candidate_path = Path(config._resolved_candidate_config_path())
    if not base_path.exists():
        return {
            "ok": False,
            "job_id": job_id,
            "reconcile_action": "none",
            "reconcile_reason": "active_config_missing",
            "fast_path": True,
        }

    current_metadata = config._scan_fwrouter_config_metadata(str(base_path))
    current_default = str(current_metadata.get("resolved_selective_default") or "").strip().lower()
    if current_default == target_default:
        return config.mihomo_runtime_satisfies_routing(routing_dict)
    if current_default not in {"direct", "vpn"}:
        return {
            "ok": False,
            "job_id": job_id,
            "reconcile_action": "none",
            "reconcile_reason": "active_metadata_not_patchable",
            "fast_path": True,
            "metadata": current_metadata,
        }

    current_transparent_rule = "MATCH,vpn-global" if current_default == "vpn" else "MATCH,DIRECT"
    expected_transparent_rule = config._build_transparent_fallback_rule(routing_dict)
    if expected_transparent_rule not in {"MATCH,DIRECT", "MATCH,vpn-global"}:
        return {
            "ok": False,
            "job_id": job_id,
            "reconcile_action": "none",
            "reconcile_reason": "target_fallback_not_patchable",
            "fast_path": True,
            "expected_transparent_final_match_rule": expected_transparent_rule,
        }

    try:
        lines = base_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        return {
            "ok": False,
            "job_id": job_id,
            "reconcile_action": "none",
            "reconcile_reason": "active_config_read_failed",
            "error": str(exc),
            "fast_path": True,
        }

    in_transparent = False
    patched_transparent = 0
    patched_metadata_default = 0
    patched_metadata_fallback = 0
    next_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if line.startswith("  fwrouter-transparent:"):
            in_transparent = True
            next_lines.append(line)
            continue
        if in_transparent and line.startswith("  ") and not line.startswith("  -"):
            in_transparent = False
        if in_transparent and stripped == f"- {current_transparent_rule}":
            next_lines.append(line.replace(current_transparent_rule, expected_transparent_rule, 1))
            patched_transparent += 1
            continue
        if line.startswith("  resolved_selective_default:"):
            next_lines.append(f"  resolved_selective_default: {target_default}\n")
            patched_metadata_default += 1
            continue
        if line.startswith("  transparent_final_match_rule:"):
            next_lines.append(f"  transparent_final_match_rule: {expected_transparent_rule}\n")
            patched_metadata_fallback += 1
            continue
        next_lines.append(line)

    if patched_transparent != 1 or patched_metadata_default != 1 or patched_metadata_fallback != 1:
        return {
            "ok": False,
            "job_id": job_id,
            "reconcile_action": "none",
            "reconcile_reason": "active_config_patch_shape_mismatch",
            "fast_path": True,
            "patched": {
                "transparent_fallback": patched_transparent,
                "metadata_default": patched_metadata_default,
                "metadata_transparent_fallback": patched_metadata_fallback,
            },
        }

    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_text(candidate_path, "".join(next_lines))
        shutil.copyfile(candidate_path, base_path)
    except OSError as exc:
        return {
            "ok": False,
            "job_id": job_id,
            "reconcile_action": "none",
            "reconcile_reason": "active_config_patch_write_failed",
            "error": str(exc),
            "fast_path": True,
        }

    restarted = config.restart_mihomo_container(action="restart")
    result = {
        "ok": bool(restarted.get("ok")),
        "job_id": job_id,
        "candidate": {
            "candidate_path": str(candidate_path),
            "rules_count": int(current_metadata.get("rendered_rules_count") or 0),
        },
        "config_validation": {
            "ok": True,
            "skipped": True,
            "reason": "fallback_only_patch",
            "resolved_selective_default": target_default,
            "transparent_final_match_rule": expected_transparent_rule,
        },
        "promoted": {
            "ok": True,
            "promoted": True,
            "reason": "fallback_only_patch",
            "base_path": str(base_path),
            "candidate_path": str(candidate_path),
        },
        "container": restarted,
        "reconcile_action": "restart",
        "reconcile_reason": "selective_default_fallback_only_patch",
        "fast_path": True,
        "state_consistency_ok": True,
        "config": _build_config_status_summary(
            base_path=str(base_path),
            candidate_path=str(candidate_path),
            candidate_rules_count=int(current_metadata.get("rendered_rules_count") or 0),
        ),
    }
    config._write_mihomo_reconcile_logs(
        ok=bool(result["ok"]),
        event_type="mihomo_selective_default_fast_reconciled"
        if result["ok"]
        else "mihomo_selective_default_fast_reconcile_failed",
        operational_level="info" if result["ok"] else "warning",
        technical_level="info" if result["ok"] else "warning",
        message="Mihomo selective_default fallback patched."
        if result["ok"]
        else "Mihomo selective_default fallback patch failed.",
        details=result,
    )
    return result


def _build_config_status_summary(
    *,
    base_path: str,
    candidate_path: str,
    base_rules_count: int | None = None,
    candidate_rules_count: int | None = None,
) -> dict[str, Any]:
    return {
        "base_path": base_path,
        "candidate_path": candidate_path,
        "base_exists": os.path.exists(base_path),
        "candidate_exists": os.path.exists(candidate_path),
        "base_updated_at": config._iso8601_mtime(base_path),
        "candidate_updated_at": config._iso8601_mtime(candidate_path),
        "base_rules_count": base_rules_count,
        "candidate_rules_count": candidate_rules_count,
    }


def promote_mihomo_candidate_config() -> dict[str, Any]:
    blocked = config.managed_runtime_operation_blocked(
        "vpn",
        error_code="MIHOMO_MANAGED_RUNTIME_REQUIRED",
        operation="mihomo_config_promote",
    )
    if blocked is not None:
        return {
            **blocked,
            "promoted": False,
            "base_path": config._resolved_base_config_path(),
            "candidate_path": config._resolved_candidate_config_path(),
        }

    candidate_path = config._resolved_candidate_config_path()
    base_path = config._resolved_base_config_path()
    if not os.path.exists(candidate_path):
        result = {
            "ok": False,
            "promoted": False,
            "error_code": "MIHOMO_CANDIDATE_MISSING",
            "error_message": "Mihomo candidate config does not exist.",
        }
        config.write_technical_log(
            component="mihomo",
            event_type="mihomo_candidate_promote_failed",
            level="warning",
            message=result["error_message"],
            details=result,
        )
        return result

    os.makedirs(os.path.dirname(base_path), exist_ok=True)
    shutil.copyfile(candidate_path, base_path)

    result = {
        "ok": True,
        "promoted": True,
        "base_path": base_path,
        "candidate_path": candidate_path,
        "status": _build_config_status_summary(
            base_path=base_path,
            candidate_path=candidate_path,
        ),
    }
    config.write_technical_log(
        component="mihomo",
        event_type="mihomo_candidate_promoted",
        level="info",
        message="Mihomo candidate config promoted to active config.",
        details=result,
    )
    return result


def validate_and_promote_mihomo_candidate_config() -> dict[str, Any]:
    blocked = config.managed_runtime_operation_blocked(
        "vpn",
        error_code="MIHOMO_MANAGED_RUNTIME_REQUIRED",
        operation="mihomo_config_validate_and_promote",
    )
    if blocked is not None:
        return {
            **blocked,
            "config": config.get_mihomo_config_status(),
            "config_validation": None,
            "container_restarted": False,
        }

    config_validation = config.validate_mihomo_candidate_config()
    if not config_validation["ok"]:
        return {
            "ok": False,
            "status": "failed",
            "stage": "config_validation",
            "error_code": "MIHOMO_CONFIG_VALIDATION_FAILED",
            "error_message": "Mihomo candidate config failed validation.",
            "config": config.get_mihomo_config_status(),
            "config_validation": config_validation,
            "container_restarted": False,
        }

    promoted = promote_mihomo_candidate_config()
    if not promoted.get("ok"):
        return {
            **promoted,
            "config": promoted,
            "config_validation": config_validation,
            "container_restarted": False,
        }

    return {
        "ok": True,
        "status": "success",
        "config": promoted,
        "config_validation": config_validation,
        "container_restarted": False,
    }


def reconcile_mihomo_runtime(routing: Any = None, job_id: str = "manual") -> dict[str, Any]:
    blocked = config.managed_runtime_operation_blocked(
        "vpn",
        error_code="MIHOMO_MANAGED_RUNTIME_REQUIRED",
        operation="mihomo_runtime_reconcile",
    )
    if blocked is not None:
        return {
            **blocked,
            "job_id": job_id,
            "candidate": None,
            "config_validation": None,
            "promoted": {
                "ok": False,
                "promoted": False,
                "reason": "managed_runtime_required",
            },
            "container": {
                "ok": False,
                "action": "none",
                "reason": "managed_runtime_required",
            },
            "reconcile_action": "none",
            "reconcile_reason": "managed_runtime_required",
            "config": config.get_mihomo_config_status(),
        }

    routing_dict = routing if isinstance(routing, dict) else None
    input_fingerprint: dict[str, Any] | None = None
    try:
        input_fingerprint = current_mihomo_input_fingerprint(routing_dict)
    except Exception:
        input_fingerprint = None
    if input_fingerprint is not None and mihomo_input_unchanged(input_fingerprint):
        base_path = config._resolved_base_config_path()
        candidate_path = config._resolved_candidate_config_path()
        return {
            "ok": True,
            "job_id": job_id,
            "candidate": {
                "skipped": True,
                "reason": "input_fingerprint_unchanged",
                "candidate_path": candidate_path,
            },
            "config_validation": {
                "ok": True,
                "skipped": True,
                "reason": "input_fingerprint_unchanged",
            },
            "promoted": {
                "ok": True,
                "promoted": False,
                "reason": "input_fingerprint_unchanged",
            },
            "container": {
                "ok": True,
                "action": "none",
                "reason": "input_fingerprint_unchanged",
            },
            "reconcile_action": "none",
            "reconcile_reason": "input_fingerprint_unchanged",
            "state_consistency_ok": True,
            "input_fingerprint": {
                "hash": input_fingerprint.get("hash"),
                "version": input_fingerprint.get("version"),
            },
            "config": _build_config_status_summary(
                base_path=base_path,
                candidate_path=candidate_path,
            ),
        }
    candidate = config.write_mihomo_candidate_config(routing_dict)
    config_validation = config.validate_mihomo_candidate_config(routing_dict)
    candidate_summary = config._summarize_candidate(candidate)
    candidate_path = str(candidate.get("candidate_path") or config._resolved_candidate_config_path())
    base_path = config._resolved_base_config_path()
    status_summary = _build_config_status_summary(
        base_path=base_path,
        candidate_path=candidate_path,
        candidate_rules_count=int(candidate_summary.get("rules_count") or 0),
    )

    if not config_validation.get("ok"):
        result = {
            "ok": False,
            "job_id": job_id,
            "candidate": candidate_summary,
            "config_validation": config_validation,
            "promoted": {
                "ok": False,
                "promoted": False,
                "reason": "validation_failed",
            },
            "container": {
                "ok": False,
                "action": "none",
                "reason": "validation_failed",
            },
            "reconcile_action": "none",
            "reconcile_reason": "validation_failed",
            "config": status_summary,
        }
        config._write_mihomo_reconcile_logs(
            ok=False,
            event_type="mihomo_reconcile_failed",
            operational_level="warning",
            technical_level="warning",
            message="Mihomo reconcile failed during candidate validation.",
            details=result,
        )
        return result

    files_match = False
    if os.path.exists(base_path) and os.path.exists(candidate_path):
        try:
            files_match = filecmp.cmp(base_path, candidate_path, shallow=False)
        except OSError:
            files_match = False
    else:
        try:
            status = config.get_mihomo_config_status(include_config=True)
        except TypeError:
            status = config.get_mihomo_config_status()
        active_config = status.get("base_config") if isinstance(status, dict) else None
        candidate_config = status.get("candidate_config") if isinstance(status, dict) else None
        files_match = config._configs_equal(active_config, candidate_config)
        status_summary = config._summarize_config_status(status)

    if files_match:
        result = {
            "ok": True,
            "job_id": job_id,
            "candidate": candidate_summary,
            "config_validation": config_validation,
            "promoted": {
                "ok": True,
                "promoted": False,
                "reason": "unchanged_config",
            },
            "container": {
                "ok": True,
                "action": "none",
                "reason": "unchanged_config",
            },
            "reconcile_action": "none",
            "reconcile_reason": "unchanged_config",
            "state_consistency_ok": True,
            "config": status_summary,
        }
        config._write_mihomo_reconcile_logs(
            ok=True,
            event_type="mihomo_reconcile_skipped",
            message="Mihomo reconcile skipped because active config already matches candidate.",
            details=result,
            operational_level="debug",
        )
        if input_fingerprint is not None:
            write_mihomo_reconcile_fingerprint_state(
                fingerprint=input_fingerprint,
                result=result,
            )
        return result

    restart_action = "force_recreate"

    promoted = promote_mihomo_candidate_config()
    restarted = config.restart_mihomo_container(action=restart_action)
    result = {
        "ok": bool(promoted.get("ok")) and bool(restarted.get("ok")),
        "job_id": job_id,
        "candidate": candidate_summary,
        "config_validation": config_validation,
        "promoted": promoted,
        "container": restarted,
        "reconcile_action": restart_action,
        "reconcile_reason": "structural_change",
        "state_consistency_ok": True,
        "config": _build_config_status_summary(
            base_path=base_path,
            candidate_path=candidate_path,
            candidate_rules_count=int(candidate_summary.get("rules_count") or 0),
        ),
    }
    config._write_mihomo_reconcile_logs(
        ok=bool(result["ok"]),
        event_type="mihomo_reconciled" if result["ok"] else "mihomo_reconcile_failed",
        operational_level="info" if result["ok"] else "warning",
        technical_level="info" if result["ok"] else "warning",
        message="Mihomo runtime reconciled." if result["ok"] else "Mihomo runtime reconcile failed after promote/restart.",
        details=result,
    )
    if result["ok"] and input_fingerprint is not None:
        write_mihomo_reconcile_fingerprint_state(
            fingerprint=input_fingerprint,
            result=result,
        )
    return result
