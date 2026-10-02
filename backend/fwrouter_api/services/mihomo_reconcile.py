from __future__ import annotations

import filecmp
import inspect
import os
import shutil
from uuid import uuid4
from pathlib import Path
from typing import Any

from fwrouter_api.services import mihomo_config as config
from fwrouter_api.services.artifacts import atomic_write_text
from fwrouter_api.services.mihomo_reconcile_fingerprint import (
    _file_hash,
    current_mihomo_input_fingerprint,
    mihomo_input_unchanged,
    write_mihomo_reconcile_fingerprint_state,
)
from fwrouter_api.adapters.xray_common import xray_writer_guard, xray_writer_guard_is_held
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision


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
    input_fingerprint = current_mihomo_input_fingerprint(routing_dict)
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

    candidate_text = "".join(next_lines)
    original_base_hash = _file_hash(base_path)
    with xray_writer_guard(timeout_seconds=30.0):
        if (
            current_mihomo_input_fingerprint(routing_dict).get("hash") != input_fingerprint.get("hash")
            or _file_hash(base_path) != original_base_hash
        ):
            return {"ok": False, "job_id": job_id, "reconcile_action": "none",
                    "reconcile_reason": "stale_selective_default_candidate", "fast_path": True,
                    "error_code": "MIHOMO_GENERATION_STALE"}
        with db_session() as connection:
            revision = read_selection_revision(connection)
            next_revision = advance_selection_revision(connection, expected_revision=revision)
        if next_revision is None:
            return {"ok": False, "job_id": job_id, "reconcile_action": "none",
                    "reconcile_reason": "selection_revision_conflict", "fast_path": True}
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write_text(candidate_path, candidate_text)
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
        restarted = config.restart_mihomo_container(
            action="restart", selection_fenced=True, expected_selection_revision=next_revision,
        )
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


def promote_mihomo_candidate_config(
    *, selection_fenced: bool = False, expected_selection_revision: int | None = None,
) -> dict[str, Any]:
    if not selection_fenced:
        with xray_writer_guard(timeout_seconds=30.0):
            with db_session() as connection:
                revision = read_selection_revision(connection)
                next_revision = advance_selection_revision(connection, expected_revision=revision)
            if next_revision is None:
                return {"ok": False, "promoted": False, "error_code": "VPN_AUTO_SELECTION_REVISION_CONFLICT"}
            result = _promote_mihomo_candidate_config_under_fence()
            return {**result, "selection_revision": next_revision}
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    if not xray_writer_guard_is_held() or type(expected_selection_revision) is not int:
        return {"ok": False, "promoted": False, "error_code": "VPN_AUTO_SELECTION_FENCE_REQUIRED"}
    with db_session() as connection:
        if read_selection_revision(connection) != expected_selection_revision:
            return {"ok": False, "promoted": False, "error_code": "VPN_AUTO_SELECTION_REVISION_CONFLICT"}
    return _promote_mihomo_candidate_config_under_fence()


def _promote_mihomo_candidate_config_under_fence() -> dict[str, Any]:
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


def _capture_mihomo_reconcile_checkpoint(base_path: str, candidate_path: str) -> dict[str, Any]:
    base = Path(base_path)
    backup = config._resolved_last_good_mihomo_dir() / "config.previous.yaml"
    if not base.is_file():
        return {"ok": False, "reason": "no_active_config", "base_path": str(base), "backup_path": str(backup)}
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.chmod(0o700)
    try:
        original = base.read_bytes()
        old_hash = _file_hash(str(base))
        atomic_write_text(backup, original.decode("utf-8"))
        backup.chmod(0o600)
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "reason": "checkpoint_write_failed", "error_message": str(exc)}
    return {
        "ok": True,
        "base_path": str(base),
        "backup_path": str(backup),
        "before_hash": old_hash,
        "candidate_hash": _file_hash(candidate_path),
    }


def _mihomo_incarnation() -> str | None:
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation

    value = get_mihomo_runtime_incarnation()
    return str(value).strip() if value else None


def _invoke_verification_callback(callback: Any, *, operation_id: str, expected_revision: int) -> Any:
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs: dict[str, Any] = {}
    accepts_any = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    if accepts_any or "operation_id" in parameters:
        kwargs["operation_id"] = operation_id
    if accepts_any or "expected_selection_revision" in parameters:
        kwargs["expected_selection_revision"] = expected_revision
    return callback(**kwargs)


def _selection_publication_still_owned(
    *, operation_id: str, expected_revision: int, expected_incarnation: str,
    expected_input_hash: str | None, expected_active_hash: str | None,
    routing: dict[str, Any] | None, verification: dict[str, Any] | None,
) -> bool:
    """Final C-phase fence before reporting verified or publishing reconcile state."""
    if not xray_writer_guard_is_held():
        return False
    try:
        current_input = current_mihomo_input_fingerprint(routing)
        current_incarnation = _mihomo_incarnation()
        active_hash = _file_hash(config._resolved_base_config_path())
        with db_session() as connection:
            current_revision = read_selection_revision(connection)
    except Exception:
        return False
    if current_revision != expected_revision:
        return False
    if current_incarnation != expected_incarnation:
        return False
    if (current_input or {}).get("hash") != expected_input_hash:
        return False
    if active_hash != expected_active_hash:
        return False
    if verification is not None:
        return bool(
            str(verification.get("operation_id") or "") == operation_id
            and type(verification.get("selection_revision")) is int
            and verification.get("selection_revision") == expected_revision
        )
    return True


def restore_mihomo_reconcile_checkpoint(
    checkpoint: dict[str, Any] | None,
    *,
    expected_revision: int | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """CAS-restore and restart the previously active config from this reconcile."""
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    if not checkpoint.get("ok"):
        return {"ok": False, "recovered": False, "reason": checkpoint.get("reason") or "checkpoint_unavailable"}
    checkpoint_revision = checkpoint.get("expected_selection_revision")
    checkpoint_operation = str(checkpoint.get("operation_id") or "")
    if (
        type(checkpoint_revision) is not int
        or expected_revision is None
        or int(expected_revision) != checkpoint_revision
        or not checkpoint_operation
        or str(operation_id or "") != checkpoint_operation
    ):
        return {"ok": False, "recovered": False, "reason": "rollback_context_missing_or_mismatched"}
    base = Path(str(checkpoint.get("base_path") or ""))
    backup = Path(str(checkpoint.get("backup_path") or ""))
    with xray_writer_guard(timeout_seconds=30.0):
        expected_incarnation = str(checkpoint.get("runtime_incarnation_after") or "")
        observed_incarnation = _mihomo_incarnation()
        if (
            not expected_incarnation
            or not observed_incarnation
            or observed_incarnation != expected_incarnation
            or not base.is_file()
            or not backup.is_file()
            or _file_hash(str(base)) != checkpoint.get("candidate_hash")
        ):
            return {"ok": False, "recovered": False, "reason": "active_config_changed"}
        with db_session() as connection:
            current_revision = read_selection_revision(connection)
            if current_revision != int(expected_revision):
                return {"ok": False, "recovered": False, "reason": "selection_revision_changed", "expected_revision": expected_revision, "current_revision": current_revision}
            rollback_revision = advance_selection_revision(connection, expected_revision=current_revision)
        if rollback_revision is None:
            return {"ok": False, "recovered": False, "reason": "selection_revision_changed"}
        try:
            atomic_write_text(base, backup.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            return {"ok": False, "recovered": False, "reason": "restore_write_failed", "error_message": str(exc), "selection_revision": rollback_revision}
        restored_hash = _file_hash(str(base))
        if restored_hash != checkpoint.get("before_hash"):
            return {"ok": False, "recovered": False, "reason": "restore_hash_mismatch", "selection_revision": rollback_revision}
        from fwrouter_api.services.mihomo_runtime import restart_mihomo_container
        try:
            restarted = restart_mihomo_container(
                action="force_recreate", selection_fenced=True,
                expected_selection_revision=rollback_revision,
            )
        except Exception as exc:
            return {"ok": False, "recovered": False, "reason": "restore_restart_failed", "error_message": str(exc), "selection_revision": rollback_revision}
        rollback_incarnation = _mihomo_incarnation()
        verified = bool(
            restarted.get("ok")
            and rollback_incarnation
            and rollback_incarnation != expected_incarnation
            and _file_hash(str(base)) == checkpoint.get("before_hash")
        )
        return {"ok": verified, "recovered": verified, "restart": restarted, "selection_revision": rollback_revision}


def reconcile_mihomo_runtime(
    routing: Any = None,
    job_id: str = "manual",
    prepared_candidate_metadata: dict[str, Any] | None = None,
    verification_callback: Any = None,
) -> dict[str, Any]:
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

    operation_id = str(uuid4())
    routing_dict = routing if isinstance(routing, dict) else None
    try:
        with db_session() as connection:
            expected_selection_revision = read_selection_revision(connection)
    except (TypeError, ValueError) as exc:
        return {
            "ok": False, "stage": "generation_snapshot",
            "error_code": "VPN_AUTO_SELECTION_FENCE_INVALID",
            "error_message": str(exc), "last_good_retained": True,
        }
    runtime_incarnation_before = _mihomo_incarnation()
    if not runtime_incarnation_before:
        return {
            "ok": False, "stage": "generation_snapshot",
            "error_code": "MIHOMO_RUNTIME_IDENTITY_UNAVAILABLE",
            "error_message": "Mihomo runtime incarnation is unavailable; refusing an unfenced generation mutation.",
            "last_good_retained": True, "operation_id": operation_id,
        }
    base_path = config._resolved_base_config_path()
    active_config_hash_before = _file_hash(base_path)
    input_fingerprint: dict[str, Any] | None = None
    try:
        input_fingerprint = current_mihomo_input_fingerprint(routing_dict)
    except Exception:
        input_fingerprint = None
    if input_fingerprint is not None and mihomo_input_unchanged(input_fingerprint):
        verification = None
        if callable(verification_callback):
            value = _invoke_verification_callback(
                verification_callback, operation_id=operation_id,
                expected_revision=expected_selection_revision,
            )
            verification = value if isinstance(value, dict) else {"ok": bool(value)}
            if not verification.get("ok"):
                return {"ok": False, "stage": "verification", "reconcile_reason": "unchanged_config", "verification_callback_result": verification, "last_good_retained": True, "promoted": {"ok": True, "promoted": False}, "container": {"ok": True, "action": "none"}}
        with xray_writer_guard(timeout_seconds=30.0):
            owned_revision = (
                verification.get("selection_revision")
                if verification is not None else expected_selection_revision
            )
            if type(owned_revision) is not int or not _selection_publication_still_owned(
                operation_id=operation_id, expected_revision=owned_revision,
                expected_incarnation=runtime_incarnation_before,
                expected_input_hash=(input_fingerprint or {}).get("hash"),
                expected_active_hash=active_config_hash_before,
                routing=routing_dict, verification=verification,
            ):
                return {"ok": False, "stage": "generation_publication_revalidation",
                        "reconcile_reason": "selection_or_runtime_superseded",
                        "error_code": "MIHOMO_GENERATION_STALE_BEFORE_PUBLICATION",
                        "verification_callback_result": verification,
                        "last_good_retained": True}
            if input_fingerprint is not None:
                write_mihomo_reconcile_fingerprint_state(fingerprint=input_fingerprint,
                                                         result={"ok": True, "reconcile_reason": "input_fingerprint_unchanged"})
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
            "verification_callback_result": verification,
            "last_good_retained": True,
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
    prepared = prepared_candidate_metadata if isinstance(prepared_candidate_metadata, dict) else None
    reuse_prepared = bool(
        prepared
        and prepared.get("input_fingerprint_hash") == (input_fingerprint or {}).get("hash")
        and prepared.get("candidate_file_hash")
        and prepared.get("candidate_file_hash") == _file_hash(config._resolved_candidate_config_path())
    )
    prepared_candidate_config = (
        prepared.get("_candidate_config")
        if reuse_prepared and bool(prepared.get("docker_validation_ok"))
        and isinstance(prepared.get("_candidate_config"), dict)
        else None
    )
    if reuse_prepared:
        candidate = {
            "candidate_path": config._resolved_candidate_config_path(),
            "reused_prepared": True,
            "file_hash": prepared.get("candidate_file_hash"),
        }
    else:
        candidate = config.write_mihomo_candidate_config(routing_dict)
    config_validation = (
        config.validate_mihomo_candidate_config(
            routing_dict,
            candidate_config=prepared_candidate_config,
        )
        if prepared_candidate_config is not None
        else config.validate_mihomo_candidate_config(routing_dict)
    )
    candidate_summary = config._summarize_candidate(candidate)
    candidate_path = str(candidate.get("candidate_path") or config._resolved_candidate_config_path())
    prepared_candidate_hash = _file_hash(candidate_path)
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
        verification = None
        if callable(verification_callback):
            try:
                value = _invoke_verification_callback(
                    verification_callback, operation_id=operation_id,
                    expected_revision=expected_selection_revision,
                )
                verification = value if isinstance(value, dict) else {"ok": bool(value)}
            except Exception as exc:
                verification = {"ok": False, "error_code": "MIHOMO_FINAL_READBACK_FAILED", "error_message": str(exc)}
        result = {
            "ok": verification is None or bool(verification.get("ok")),
            "stage": "verification" if verification is not None and not verification.get("ok") else None,
            "verification_callback_result": verification,
            "last_good_retained": True,
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
            ok=bool(result["ok"]),
            event_type="mihomo_reconcile_skipped" if result["ok"] else "mihomo_reconcile_failed",
            message="Mihomo reconcile skipped because active config already matches candidate.",
            details=result,
            operational_level="debug",
        )
        if result["ok"]:
            with xray_writer_guard(timeout_seconds=30.0):
                owned_revision = verification.get("selection_revision") if verification else expected_selection_revision
                if type(owned_revision) is not int or not _selection_publication_still_owned(
                    operation_id=operation_id, expected_revision=owned_revision,
                    expected_incarnation=runtime_incarnation_before,
                    expected_input_hash=(input_fingerprint or {}).get("hash"),
                    expected_active_hash=active_config_hash_before, routing=routing_dict,
                    verification=verification,
                ):
                    result.update({"ok": False, "stage": "generation_publication_revalidation",
                                   "reconcile_reason": "selection_or_runtime_superseded",
                                   "error_code": "MIHOMO_GENERATION_STALE_BEFORE_PUBLICATION",
                                   "last_good_retained": True})
                elif input_fingerprint is not None:
                    write_mihomo_reconcile_fingerprint_state(fingerprint=input_fingerprint, result=result)
        return result

    restart_action = "force_recreate"

    with xray_writer_guard(timeout_seconds=30.0):
        try:
            current_fingerprint = current_mihomo_input_fingerprint(routing_dict)
            current_incarnation = _mihomo_incarnation()
        except Exception as exc:
            return {
                "ok": False, "stage": "generation_revalidation",
                "error_code": "MIHOMO_INPUT_REVALIDATION_FAILED",
                "error_message": str(exc), "last_good_retained": True,
                "reconcile_action": "none",
            }
        recovery_checkpoint = (
            _capture_mihomo_reconcile_checkpoint(base_path, candidate_path)
            if callable(verification_callback)
            else {"ok": False, "reason": "verification_callback_not_requested"}
        )
        with db_session() as connection:
            current_revision = read_selection_revision(connection)
            if (
                current_revision != expected_selection_revision
                or (input_fingerprint or {}).get("hash") != (current_fingerprint or {}).get("hash")
                or not prepared_candidate_hash
                or _file_hash(candidate_path) != prepared_candidate_hash
                or current_incarnation != runtime_incarnation_before
            ):
                return {
                    "ok": False, "stage": "generation_revalidation",
                    "error_code": "MIHOMO_GENERATION_STALE",
                    "error_message": "Selection state, source fingerprint, or candidate changed during preparation.",
                    "last_good_retained": True, "reconcile_action": "none",
                    "reconcile_reason": "stale_generation_candidate",
                    "operation_id": operation_id,
                    "expected_selection_revision": expected_selection_revision,
                    "current_selection_revision": current_revision,
                }
            generation_revision = advance_selection_revision(
                connection, expected_revision=current_revision
            )
        if generation_revision is None:
            return {
                "ok": False,
                "stage": "generation_fence",
                "error_code": "VPN_AUTO_SELECTION_REVISION_CONFLICT",
                "last_good_retained": True,
                "reconcile_action": "none",
                "reconcile_reason": "generation_fence_conflict",
            }
        recovery_checkpoint.update({
            "expected_selection_revision": generation_revision,
            "operation_id": operation_id,
            "input_fingerprint_hash": (input_fingerprint or {}).get("hash"),
            "candidate_hash": prepared_candidate_hash,
            "runtime_incarnation_before": runtime_incarnation_before,
        })
        promoted = promote_mihomo_candidate_config(
            selection_fenced=True, expected_selection_revision=generation_revision,
        )
        restarted = config.restart_mihomo_container(
            action=restart_action, selection_fenced=True,
            expected_selection_revision=generation_revision,
        )
        recovery_checkpoint["runtime_incarnation_after"] = _mihomo_incarnation()
    verification = None
    if bool(promoted.get("ok")) and bool(restarted.get("ok")) and callable(verification_callback):
        try:
            value = _invoke_verification_callback(
                verification_callback, operation_id=operation_id,
                expected_revision=generation_revision,
            )
            verification = value if isinstance(value, dict) else {"ok": bool(value)}
        except Exception as exc:
            verification = {"ok": False, "error_code": "MIHOMO_FINAL_READBACK_FAILED", "error_message": str(exc)}
    verified = bool(promoted.get("ok")) and bool(restarted.get("ok")) and (verification is None or bool(verification.get("ok")))
    recovery = None
    recovery_expected_revision = generation_revision
    if (
        verification
        and str(verification.get("operation_id") or "") == operation_id
        and type(verification.get("selection_revision")) is int
    ):
        # The Core commit is an owned phase even if a later provider/readback
        # verification fails. Adopt only its explicit revision for safe rollback;
        # terminal callback success is a separate concern.
        recovery_expected_revision = int(verification["selection_revision"])
    recovery_checkpoint["expected_selection_revision"] = recovery_expected_revision
    runtime_incarnation_after = str(recovery_checkpoint.get("runtime_incarnation_after") or "")
    publication_owned = False
    publication_revalidation_failed = False
    if verified:
        with xray_writer_guard(timeout_seconds=30.0):
            publication_owned = _selection_publication_still_owned(
                operation_id=operation_id,
                expected_revision=recovery_expected_revision,
                expected_incarnation=runtime_incarnation_after,
                expected_input_hash=(input_fingerprint or {}).get("hash"),
                expected_active_hash=prepared_candidate_hash,
                routing=routing_dict,
                verification=verification,
            )
            if publication_owned and input_fingerprint is not None:
                write_mihomo_reconcile_fingerprint_state(
                    fingerprint=input_fingerprint,
                    result={"ok": True, "reconcile_reason": "structural_change", "operation_id": operation_id},
                )
        if not publication_owned:
            publication_revalidation_failed = True
            verified = False
    if not verified and promoted.get("promoted"):
        recovery = restore_mihomo_reconcile_checkpoint(
            recovery_checkpoint,
            expected_revision=recovery_expected_revision,
            operation_id=operation_id,
        )
    result = {
        "ok": verified,
        "stage": ("generation_publication_revalidation" if publication_revalidation_failed else
                  ("verification" if verification is not None and not verification.get("ok") else None)),
        "error_code": "MIHOMO_GENERATION_STALE_BEFORE_PUBLICATION" if publication_revalidation_failed else None,
        "verification_callback_result": verification,
        "last_good_retained": bool(recovery.get("ok")) if recovery is not None else (not bool(promoted.get("promoted")) if not promoted.get("ok") else False),
        "generation_recovery": recovery,
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
    return result
