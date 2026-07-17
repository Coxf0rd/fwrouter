from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptRunnerError
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.jobs import cleanup_stale_running_jobs
from fwrouter_api.services.dataplane_live import live_mode_matches_intent, probe_live_global_mode
from fwrouter_api.services.dataplane_live import applied_nft_markers_match_live
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.system_subjects import ensure_builtin_system_subjects


def get_bootstrap_directories() -> list[Path]:
    """Return backend-owned directories that are safe to create at startup."""

    paths = get_settings().paths

    return [
        paths.state_dir,
        paths.rules_dir,
        paths.generated_dir,
        paths.generated_dir / "dataplane",
        paths.generated_dir / "mihomo",
        paths.jobs_dir,
        paths.cache_dir,
        paths.runtime_state_dir,
        paths.log_dir,
        paths.operational_log_dir,
        paths.technical_log_dir,
        paths.run_dir,
    ]


def ensure_bootstrap_directories() -> list[str]:
    """Create backend-owned state/log/runtime directories.

    This intentionally does not create or modify /etc/fwrouter live config
    directories. Live config layout is handled later by explicit apply/bootstrap
    jobs with confirmation.
    """

    created_or_existing: list[str] = []

    for directory in get_bootstrap_directories():
        directory.mkdir(parents=True, exist_ok=True)
        created_or_existing.append(str(directory))

    return created_or_existing


def normalize_subject_taxonomy() -> dict[str, Any]:
    """Normalize legacy subject types to current canonical taxonomy."""

    with db_session() as connection:
        legacy_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT subject_id, subject_type, stable_key, display_name
                FROM subjects
                WHERE subject_type = 'tailscale'
                ORDER BY created_at, subject_id
                """
            ).fetchall()
        ]

        normalized_count = 0
        if legacy_rows:
            normalized_count = connection.execute(
                """
                UPDATE subjects
                SET
                    subject_type = 'tailscale_node',
                    updated_at = CURRENT_TIMESTAMP
                WHERE subject_type = 'tailscale'
                """
            ).rowcount

    result = {
        "legacy_tailscale_subjects_count": len(legacy_rows),
        "normalized_tailscale_node_count": normalized_count,
        "subjects": legacy_rows,
    }

    if normalized_count > 0:
        write_technical_log(
            component="bootstrap",
            event_type="subject_taxonomy_normalized",
            message="Legacy tailscale subjects were normalized to tailscale_node.",
            details=result,
        )

    return result


def _read_startup_dataplane_payload() -> dict[str, Any] | None:
    settings = get_settings()
    applied_manifest_path = settings.paths.generated_dir / "dataplane" / "applied-manifest.json"
    applied_nft_path = settings.paths.generated_dir / "dataplane" / "applied.nft"
    last_good_nft_path = settings.paths.state_dir / "last-good" / "dataplane" / "last-good.nft"

    if not applied_manifest_path.exists():
        return None

    extra_args: list[str] = []
    if last_good_nft_path.exists():
        extra_args.append(str(last_good_nft_path))
    extra_args.append(str(applied_manifest_path))

    try:
        script_result = DEFAULT_SCRIPT_RUNNER.run("dataplane_check", extra_args=extra_args)
    except ScriptRunnerError:
        return None

    try:
        payload = json.loads(script_result.stdout or "{}")
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None
    payload["artifact_consistency"] = applied_nft_markers_match_live(applied_nft_path)
    return payload


def _live_dataplane_missing(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return True
    if not bool(payload.get("table_exists")):
        return True
    required_chains = payload.get("required_chains")
    if not isinstance(required_chains, dict):
        return True
    if not all(bool(required_chains.get(chain)) for chain in required_chains):
        return True
    artifact_consistency = payload.get("artifact_consistency")
    return isinstance(artifact_consistency, dict) and not bool(artifact_consistency.get("ok", True))


def _read_persisted_global_routing_intent() -> dict[str, Any]:
    """Return persisted global routing intent from SQLite state.

    This is the startup source of truth for what the user intended to keep
    across reboot. It is distinct from the current live Linux/Mihomo state.
    """

    from fwrouter_api.services.servers import ensure_routing_global_state

    routing = ensure_routing_global_state()
    intended_mode = str(
        routing.get("applied_mode") or routing.get("desired_mode") or "direct"
    ).strip().lower()
    selective_default = str(routing.get("selective_default") or "direct").strip().lower()

    return {
        "routing": routing,
        "intended_mode": intended_mode,
        "selective_default": selective_default,
    }


def _startup_live_recovery_mode_for_persisted_intent(intended_mode: str) -> str:
    return intended_mode if intended_mode in {"selective", "vpn"} else "direct"


def recover_startup_live_routing_from_persisted_mode() -> dict[str, Any]:
    """Restore live routing from persisted intent when boot lost dataplane state.

    nftables state and policy routing do not survive reboot. Startup must
    restore only the minimum live routing contour needed to match persisted
    global intent again. This is startup recovery, not a full project-wide
    apply/reconcile pipeline.

    What this does:
    - checks whether the live dataplane is missing after reboot
    - reads persisted global routing intent from SQLite
    - restores a direct/selective/vpn live contour from that persisted intent

    What this does not do:
    - it does not rewrite persisted routing intent
    - it does not perform broad system bootstrap or config regeneration
    - it does not claim that all startup reconciliation is complete
    """

    from fwrouter_api.services.apply_orchestrator import apply_global_mode_immediately

    persisted_intent = _read_persisted_global_routing_intent()
    payload = _read_startup_dataplane_payload()
    recovery_required = _live_dataplane_missing(payload)
    intended_mode = str(persisted_intent["intended_mode"])
    recovery_mode = _startup_live_recovery_mode_for_persisted_intent(intended_mode)
    recovery_requested_by = (
        "startup-intended-recovery"
        if recovery_mode != "direct"
        else "startup-direct-recovery"
    )

    result = {
        "recovery_required": recovery_required,
        "persisted_intent": persisted_intent,
        "intended_mode": intended_mode,
        "recovery_mode": recovery_mode,
        "dataplane_payload": payload,
        "recovered": False,
    }

    if not recovery_required:
        return result

    recovery = apply_global_mode_immediately(
        recovery_mode,
        requested_by=recovery_requested_by,
    )
    result["recovery"] = recovery
    result["recovered"] = bool(recovery.get("ok"))

    write_technical_log(
        component="bootstrap",
        event_type="startup_live_routing_recovered",
        message="Startup restored a live routing contour because dataplane was absent after boot.",
        details=result,
    )
    return result


def recover_startup_routing_to_direct() -> dict[str, Any]:
    """Compatibility alias for startup live recovery from persisted mode."""

    return recover_startup_live_routing_from_persisted_mode()


def recover_startup_mihomo_selector() -> dict[str, Any]:
    """Restore the intended Mihomo vpn-global selector after backend restart.

    The Mihomo container can outlive the backend process, so its selector state
    may drift from SQLite routing_global_state. Keep this startup action limited
    to selector restore only; it must not change nftables/routing on the host.
    """

    from fwrouter_api.services.selector import restore_mihomo_selector_state

    try:
        result = restore_mihomo_selector_state(requested_by="startup_restore")
    except Exception as exc:  # pragma: no cover - startup fallback path
        result = {
            "restored": False,
            "error": str(exc),
        }
        write_technical_log(
            component="bootstrap",
            event_type="startup_mihomo_selector_restore_failed",
            message="Failed to restore Mihomo selector state during backend startup.",
            details=result,
        )
        return result

    result["restored"] = bool(result.get("ok"))

    write_technical_log(
        component="bootstrap",
        event_type="startup_mihomo_selector_restored",
        message="Backend startup restored the intended Mihomo vpn-global selector.",
        details=result,
    )
    return result


def recover_startup_intended_routing() -> dict[str, Any]:
    """Re-apply persisted non-direct intent when live dataplane drifted on startup.

    This step runs after minimal startup live recovery. It is intentionally
    narrower than a full backend reconcile and exists to bring live mode back
    in sync with persisted intent when boot/restart left Mihomo/nft state stale.
    When drift is confirmed, this step may call the apply orchestrator to
    converge live state back to the persisted non-direct mode.
    """

    from fwrouter_api.services.apply_orchestrator import apply_global_mode_immediately
    from fwrouter_api.adapters.mihomo import MihomoRuntimeState

    persisted_intent = _read_persisted_global_routing_intent()
    intended_mode = str(persisted_intent["intended_mode"])
    selective_default = str(persisted_intent["selective_default"])
    live_probe = probe_live_global_mode()
    mode_matches = live_mode_matches_intent(
        expected_mode=intended_mode,
        expected_selective_default=selective_default,
        probe=live_probe,
    )

    result = {
        "persisted_intent": persisted_intent,
        "intended_mode": intended_mode,
        "selective_default": selective_default,
        "live_probe": live_probe,
        "mode_matches": mode_matches,
        "reapply_required": intended_mode != "direct" and not mode_matches,
        "reapplied": False,
    }

    if not result["reapply_required"]:
        return result

    if intended_mode in {"selective", "vpn"}:
        from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER

        health = DEFAULT_MIHOMO_ADAPTER.health()
        runtime_state_value = (
            health.runtime_state.value
            if isinstance(health.runtime_state, MihomoRuntimeState)
            else str(health.runtime_state)
        )
        result["mihomo_health"] = {
            "runtime_state": runtime_state_value,
            "message": health.message,
            "details": health.details,
        }
        if runtime_state_value != MihomoRuntimeState.RUNNING.value:
            result["reapply_required"] = False
            result["reapply_skipped"] = True
            result["reapply_skip_reason"] = "mihomo_controller_unreachable"
            return result

    recovery = apply_global_mode_immediately(intended_mode, requested_by="startup-intended-recovery")
    result["recovery"] = recovery
    result["reapplied"] = bool(recovery.get("ok"))

    write_technical_log(
        component="bootstrap",
        event_type="startup_intended_routing_reapplied",
        message="Backend startup re-applied intended routing mode because live dataplane drifted.",
        details=result,
    )
    return result


def _read_persisted_scoped_routing_subjects() -> list[dict[str, Any]]:
    """Return active LAN/Tailscale subjects whose scoped mode must be in nft."""

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT subject_id, subject_type, desired_mode, applied_mode, apply_state
            FROM subjects
            WHERE is_active = 1
              AND is_deleted = 0
              AND subject_type IN ('lan', 'tailscale', 'tailscale_node')
              AND desired_mode IN ('direct', 'selective', 'vpn')
            ORDER BY updated_at DESC, subject_id
            """
        ).fetchall()

    return [dict(row) for row in rows]


def _read_live_classify_chain() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "raw_chain": "",
            "error_code": "NFT_NOT_AVAILABLE",
            "error_message": str(exc),
        }
    except subprocess.CalledProcessError as exc:
        return {
            "ok": False,
            "raw_chain": exc.stdout or "",
            "error_code": "NFT_CHAIN_READ_FAILED",
            "error_message": (exc.stderr or exc.stdout or str(exc)).strip(),
        }

    return {"ok": True, "raw_chain": completed.stdout, "error_code": None, "error_message": None}


def recover_startup_scoped_subject_routing() -> dict[str, Any]:
    """Re-materialize persisted scoped client rules after backend restart.

    Global live mode can legitimately be `direct` while selected LAN/Tailscale
    clients are scoped `selective`/`vpn`. Startup drift checks that only inspect
    the global fallback therefore miss a broken live dataplane where SQLite says
    the subject is applied but `fwrouter_classify` lacks the scoped rules.
    """

    subjects = _read_persisted_scoped_routing_subjects()
    classify = _read_live_classify_chain()
    raw_chain = str(classify.get("raw_chain") or "")
    missing_subjects = [
        subject
        for subject in subjects
        if str(subject.get("subject_id") or "") not in raw_chain
    ]
    result = {
        "persisted_scoped_subjects_count": len(subjects),
        "missing_scoped_subjects_count": len(missing_subjects),
        "missing_subject_ids": [str(subject.get("subject_id")) for subject in missing_subjects],
        "classify_probe_ok": bool(classify.get("ok")),
        "classify_error_code": classify.get("error_code"),
        "reapply_required": bool(subjects and (not classify.get("ok") or missing_subjects)),
        "reapplied": False,
    }

    if not result["reapply_required"]:
        return result

    target = missing_subjects[0] if missing_subjects else subjects[0]
    subject_id = str(target.get("subject_id") or "")
    mode = str(target.get("desired_mode") or target.get("applied_mode") or "").strip().lower()
    if not subject_id or mode not in {"direct", "selective", "vpn"}:
        result["reapply_skipped"] = True
        result["reapply_skip_reason"] = "invalid_scoped_subject_recovery_target"
        return result

    from fwrouter_api.services.apply_orchestrator import set_subject_admin_mode

    recovery = set_subject_admin_mode(
        subject_id,
        mode,
        requested_by="startup-scoped-subject-recovery",
    )
    result["recovery"] = recovery
    result["reapplied"] = bool(recovery.get("ok"))

    write_technical_log(
        component="bootstrap",
        event_type="startup_scoped_subject_routing_reapplied",
        message="Backend startup re-applied scoped subject routing because live nft rules were missing.",
        details=result,
    )
    return result


def _startup_recovery_skipped_result() -> dict[str, Any]:
    return {"skipped": True, "reason": "startup_recovery_disabled"}


def _run_startup_live_recovery_steps(*, enabled: bool) -> dict[str, Any]:
    """Run only bounded startup live-recovery steps controlled by the env flag.

    `FWROUTER_STARTUP_RECOVERY_ENABLED` is intentionally limited to a small
    recovery scope:
    - restore live routing from persisted mode when reboot lost dataplane state
    - restore the Mihomo selector from persisted state

    It is not a generic "fix everything at startup" switch.
    """

    if not enabled:
        skipped = _startup_recovery_skipped_result()
        return {
            "scope": "bounded_startup_live_recovery",
            "live_routing": skipped,
            "selector": skipped,
        }

    return {
        "scope": "bounded_startup_live_recovery",
        "live_routing": recover_startup_live_routing_from_persisted_mode(),
        "selector": recover_startup_mihomo_selector(),
    }


def _run_startup_apply_reconcile_steps(*, enabled: bool) -> dict[str, Any]:
    """Run startup steps that may do apply/reconcile work.

    These are intentionally separate from safe bootstrap and from bounded
    live-recovery steps because they may perform stronger convergence actions:
    - re-apply persisted non-direct routing intent via apply orchestrator
    - reconcile dnsmasq runtime state

    The env flag remains unchanged; this split only makes the boundary explicit.
    """

    intended_routing = (
        recover_startup_intended_routing()
        if enabled
        else _startup_recovery_skipped_result()
    )
    scoped_subject_routing = (
        recover_startup_scoped_subject_routing()
        if enabled
        else _startup_recovery_skipped_result()
    )
    dnsmasq_reconcile = _run_startup_dnsmasq_reconcile_step()

    return {
        "scope": "startup_apply_reconcile",
        "intended_routing": intended_routing,
        "scoped_subject_routing": scoped_subject_routing,
        "dnsmasq_reconcile": dnsmasq_reconcile,
    }


def _run_startup_dnsmasq_reconcile_step() -> dict[str, Any]:
    """Run the standalone dnsmasq startup reconcile step.

    This remains a separate startup action so code makes it explicit that
    dnsmasq reconcile is not the same thing as persisted-mode recovery and is
    not part of the always-safe bootstrap foundation.
    """

    try:
        from fwrouter_api.services.dnsmasq import reconcile_dnsmasq_rules

        return reconcile_dnsmasq_rules()
    except Exception as exc:  # pragma: no cover - defensive startup path
        result = {
            "ok": False,
            "error_code": "STARTUP_DNSMASQ_RECONCILE_FAILED",
            "message": str(exc),
        }
        write_technical_log(
            component="bootstrap",
            event_type="startup_dnsmasq_reconcile_failed",
            message="Backend startup failed to reconcile dnsmasq rules.",
            details=result,
        )
        return result


def bootstrap_backend() -> dict[str, Any]:
    """Initialize safe backend foundation.

    Always-safe bootstrap:
    - prepare backend-owned directories
    - initialize SQLite schema
    - clean stale jobs
    - normalize basic persistent subject state

    Separate startup phases:
    - bounded live recovery, gated by `FWROUTER_STARTUP_RECOVERY_ENABLED`
    - startup apply/reconcile steps, kept explicit so bootstrap does not read
      like a hidden "apply everything" entrypoint
    """

    directories = ensure_bootstrap_directories()
    schema_state = initialize_database()
    stale_jobs = cleanup_stale_running_jobs(stale_after_seconds=0)
    if schema_state["ok"]:
        subject_taxonomy = normalize_subject_taxonomy()
        builtin_system_subjects = ensure_builtin_system_subjects()
    else:
        subject_taxonomy = {
            "legacy_tailscale_subjects_count": 0,
            "normalized_tailscale_node_count": 0,
            "subjects": [],
            "skipped": True,
            "reason": "database_schema_mismatch",
        }
        builtin_system_subjects = []
        write_technical_log(
            component="bootstrap",
            event_type="database_schema_mismatch_detected",
            message="SQLite schema drift detected during backend bootstrap.",
            details=schema_state,
        )

    settings = get_settings()
    startup_recovery_enabled = bool(settings.startup_recovery_enabled)
    startup_live_recovery_steps = _run_startup_live_recovery_steps(
        enabled=startup_recovery_enabled
    )
    startup_apply_reconcile_steps = _run_startup_apply_reconcile_steps(
        enabled=startup_recovery_enabled
    )

    return {
        "directories": directories,
        "database_initialized": True,
        "database_schema": schema_state,
        "stale_jobs_cleaned": stale_jobs,
        "startup_foundation": {
            "scope": "safe_startup_foundation",
            "directories": directories,
            "database_initialized": True,
            "database_schema": schema_state,
            "stale_jobs_cleaned": stale_jobs,
        },
        "subject_taxonomy": subject_taxonomy,
        "builtin_system_subjects": {
            "normalized_subject_ids": builtin_system_subjects,
            "normalized_count": len(builtin_system_subjects),
        },
        "startup_recovery_enabled": startup_recovery_enabled,
        "startup_recovery_scope": startup_live_recovery_steps["scope"],
        "startup_apply_reconcile_scope": startup_apply_reconcile_steps["scope"],
        "startup_live_recovery": startup_live_recovery_steps,
        "startup_apply_reconcile": startup_apply_reconcile_steps,
        "startup_recovery": startup_live_recovery_steps["live_routing"],
        "startup_live_routing_recovery": startup_live_recovery_steps["live_routing"],
        "startup_selector_recovery": startup_live_recovery_steps["selector"],
        "startup_intended_routing_recovery": startup_apply_reconcile_steps["intended_routing"],
        "startup_scoped_subject_routing_recovery": startup_apply_reconcile_steps["scoped_subject_routing"],
        "startup_dnsmasq_reconcile": startup_apply_reconcile_steps["dnsmasq_reconcile"],
    }
