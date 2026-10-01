from __future__ import annotations

import subprocess
from time import perf_counter
from typing import Any

from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.mihomo_config import (
    MIHOMO_CANDIDATE_CONFIG_PATH,
    reconcile_mihomo_runtime,
    write_mihomo_candidate_config,
)
from fwrouter_api.services.mihomo_config_status import _summarize_candidate
from fwrouter_api.services.mihomo_reconcile_fingerprint import (
    _file_hash,
    current_mihomo_input_fingerprint,
    mihomo_input_unchanged,
)
from fwrouter_api.services.selector import get_vpn_auto_state, select_vpn_auto_server
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.subscription import (
    _source_id,
    _subscription_sources,
    refresh_all_subscriptions as refresh_all_subscription_inventory,
    refresh_subscription as refresh_one_subscription_inventory,
    redact_subscription_public_value,
    safe_subscription_source_labels,
)


MIHOMO_IMAGE = "metacubex/mihomo:v1.19.31"


def _validate_generated_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return validate_mihomo_candidate_config()


def _write_generated_candidate() -> dict[str, Any]:
    return write_mihomo_candidate_config(include_internal_config=True)


def _public_prepared_result(prepared: dict[str, Any]) -> dict[str, Any]:
    public = dict(prepared)
    metadata = public.get("prepared_candidate_metadata")
    if isinstance(metadata, dict):
        public["prepared_candidate_metadata"] = {
            key: value for key, value in metadata.items() if not str(key).startswith("_")
        }
    candidate = public.get("candidate")
    if isinstance(candidate, dict):
        candidate_public = dict(candidate)
        for key in ("config", "_candidate_config", "rules", "handoff_assignments"):
            candidate_public.pop(key, None)
        public["candidate"] = candidate_public
    return public


def _maybe_select_vpn_auto_after_refresh() -> dict[str, Any]:
    from fwrouter_api.services.provider_managed import provider_selection_request
    requested_logical = provider_selection_request()
    if requested_logical:
        selector = select_vpn_auto_server(apply=True, check_on_demand=True, exclude_active=False,
            post_check=True, origin="subscription", reason="subscription_refresh_auto_select",
            candidate_server_id=requested_logical)
        verified = bool(selector.get("ok") and selector.get("selection_outcome") in {"selected", "noop"})
        return {"ok": verified, "triggered": True, "status": "provider_target_verified" if verified else "provider_target_unconfirmed", "selector": selector}
    routing = get_routing_global_state() or {}
    mode = str(routing.get("server_mode") or "auto").strip().lower()
    if mode == "fixed":
        fixed_id = str(routing.get("desired_fixed_server_id") or routing.get("applied_fixed_server_id") or "").strip()
        if not fixed_id:
            return {"ok": True, "triggered": False, "status": "skipped_no_fixed_target"}
        from fwrouter_api.services.logical_topology import get_logical_runtime_name
        state = get_vpn_auto_state(read_only=True)
        expected = get_logical_runtime_name(fixed_id)
        selectors = state.get("selector_runtime") if isinstance(state.get("selector_runtime"), dict) else {}
        effective = str(selectors.get("vpn_global_now") or "").strip()
        verified = bool(fixed_id and expected and effective and expected == effective)
        return {
            "ok": verified,
            "triggered": False,
            "status": "verified_fixed_target" if verified else "unconfirmed_fixed_target",
            "error_code": None if verified else "SUBSCRIPTION_FIXED_TARGET_READBACK_UNCONFIRMED",
            "error_message": None if verified else "The saved fixed logical server does not match the effective runtime target.",
            "logical_server_id": fixed_id or None,
            "expected_effective_target": expected,
            "effective_target": effective or None,
            "state": state,
        }
    if mode != "auto":
        return {
            "ok": True,
            "triggered": False,
            "status": "skipped_not_auto_mode",
            "state": get_vpn_auto_state(),
        }

    state = get_vpn_auto_state()
    if int(state.get("auto_selectable_candidates_count") or 0) <= 0:
        if not str(state.get("active_auto_server_id") or "").strip():
            return {"ok": True, "triggered": False, "status": "skipped_no_active_selection", "state": state}
        return {
            "ok": False,
            "triggered": False,
            "status": "pending_no_auto_selectable_candidates",
            "error_code": "VPN_AUTO_NO_ELIGIBLE_ALTERNATIVE",
            "error_message": "The selected VPN-auto server is unavailable and no eligible alternative can be applied.",
            "state": state,
        }

    if bool(state.get("active_auto_server_valid")):
        logical_id = str(state.get("active_auto_server_id") or "").strip()
        candidate_targets = dict(zip(
            [str(value) for value in state.get("auto_selectable_candidate_ids") or []],
            [str(value) for value in state.get("auto_selectable_candidate_target_names") or []],
        ))
        selectors = state.get("selector_runtime") if isinstance(state.get("selector_runtime"), dict) else {}
        effective = str(selectors.get("vpn_auto_now") or "").strip()
        expected = candidate_targets.get(logical_id) or logical_id
        verified = bool(logical_id and effective and effective == expected)
        return {
            "ok": verified,
            "triggered": False,
            "status": "skipped_existing_valid_active" if verified else "unconfirmed_existing_active",
            "error_code": None if verified else "VPN_AUTO_READBACK_UNCONFIRMED",
            "error_message": None if verified else "Current logical and effective VPN-auto selection do not match after runtime reconciliation.",
            "logical_server_id": logical_id or None,
            "expected_effective_target": expected or None,
            "effective_target": effective or None,
            "state": state,
        }

    selector = select_vpn_auto_server(
        apply=True,
        check_on_demand=True,
        exclude_active=bool(state.get("active_auto_server_id")),
        reason="subscription_refresh_auto_select",
        post_check=True,
        origin="subscription",
    )
    state_after = get_vpn_auto_state()
    selected_server_id = str(selector.get("selected_server_id") or "").strip()
    selectable_ids = {str(value) for value in (state_after.get("auto_selectable_candidate_ids") or [])}
    selected_target_ready = bool(
        selector.get("ok")
        and selected_server_id
        and selected_server_id in selectable_ids
        and str(state_after.get("active_auto_server_id") or "") == selected_server_id
        and bool(state_after.get("active_auto_server_valid"))
    )
    candidate_targets = dict(zip(
        [str(value) for value in state_after.get("auto_selectable_candidate_ids") or []],
        [str(value) for value in state_after.get("auto_selectable_candidate_target_names") or []],
    ))
    runtime_selectors = state_after.get("selector_runtime") if isinstance(state_after.get("selector_runtime"), dict) else {}
    effective_target = str(runtime_selectors.get("vpn_auto_now") or "").strip()
    expected_effective = candidate_targets.get(selected_server_id) or selected_server_id
    selected_target_ready = selected_target_ready and bool(effective_target and effective_target == expected_effective)
    return {
        "ok": selected_target_ready,
        "triggered": True,
        "status": "auto_selected" if selected_target_ready else "pending_auto_select",
        "error_code": None if selected_target_ready else "VPN_AUTO_SELECTED_TARGET_NOT_APPLIED",
        "error_message": None if selected_target_ready else "Selected VPN-auto target is not present in the applied eligible runtime group.",
        "logical_server_id": str(state_after.get("active_auto_server_id") or "") or None,
        "expected_effective_target": expected_effective or None,
        "effective_target": effective_target or None,
        "selector": selector,
        "state": state_after,
    }


def _subscription_transition_preflight() -> dict[str, Any]:
    """Reject a refresh that would strand the persisted current target."""
    from fwrouter_api.db.connection import db_session
    with db_session() as connection:
        row = connection.execute(
            "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, active_auto_server_id FROM routing_global_state WHERE id = 1"
        ).fetchone()
        active_ids = {
            str(item[0])
            for item in connection.execute("SELECT server_id FROM servers WHERE inventory_state = 'active'").fetchall()
        }
    routing = dict(row) if row is not None else {}
    mode = str(routing.get("server_mode") or "auto").lower()
    if mode == "fixed":
        selected = str(routing.get("desired_fixed_server_id") or routing.get("applied_fixed_server_id") or "").strip()
        if selected and selected not in active_ids:
            return {"ok": False, "stage": "preflight", "error": {"code": "SUBSCRIPTION_FIXED_TARGET_ORPHANED", "message": "The saved fixed server is no longer present in subscription inventory. The last verified runtime remains active."}}
        return {"ok": True, "mode": "fixed", "selected_server_id": selected or None}
    active_auto = str(routing.get("active_auto_server_id") or "").strip()
    if not active_auto:
        return {"ok": True, "mode": "auto", "selected_server_id": None}
    state = get_vpn_auto_state(read_only=True)
    if active_auto in active_ids and bool(state.get("active_auto_server_valid")):
        return {"ok": True, "mode": "auto", "selected_server_id": active_auto}
    alternatives = [
        str(value) for value in state.get("auto_selectable_candidate_ids") or []
        if str(value) and str(value) != active_auto
    ]
    if not alternatives:
        return {"ok": False, "stage": "preflight", "error": {"code": "VPN_AUTO_NO_ELIGIBLE_ALTERNATIVE", "message": "The selected VPN-auto server is unavailable and no eligible alternative can be applied. The last verified runtime remains active."}}
    return {"ok": True, "mode": "auto", "selected_server_id": active_auto, "alternative_count": len(alternatives)}


def _source_outcomes(refresh: dict[str, Any], *, outcome: str | None = None) -> list[dict[str, Any]]:
    state = refresh.get("state") if isinstance(refresh.get("state"), dict) else {}
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    labels = safe_subscription_source_labels(metadata)
    sources = _subscription_sources(metadata)
    batch = refresh.get("batch") if isinstance(refresh.get("batch"), dict) else {}
    batch_items = batch.get("items") if isinstance(batch.get("items"), list) else []
    by_ref: dict[str, dict[str, Any]] = {}
    for item in batch_items:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        ref = _source_id(str(item["url"]))
        error = item.get("error") if isinstance(item.get("error"), dict) else {}
        succeeded = bool(item.get("ok"))
        by_ref[ref] = {
            "outcome": "success" if succeeded else "failed",
            "error_code": str(error.get("code") or "") or None,
            "error_message": None if succeeded else str(redact_subscription_public_value(error.get("message") or "Source refresh failed.")),
        }
    result = []
    for source in sources:
        url = str(source.get("url") or "")
        ref = _source_id(url)
        source_result = by_ref.get(ref)
        if source_result is None:
            source_result = {"outcome": "skipped", "error_code": None, "error_message": None}
        retained = bool(source.get("used_last_good")) or source_result["outcome"] == "skipped"
        result.append({
            "source_ref": ref,
            "display_label": labels.get(ref, ""),
            **source_result,
            "retained": retained,
        })
    return result


def _subscription_intent_saved(refresh: dict[str, Any]) -> bool:
    state = refresh.get("state") if isinstance(refresh.get("state"), dict) else {}
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    sources = _subscription_sources(metadata)
    batch = refresh.get("batch") if isinstance(refresh.get("batch"), dict) else {}
    target = str(batch.get("targeted_source_ref") or "").strip()
    refs = {_source_id(str(source.get("url") or "")) for source in sources if source.get("url")}
    if target:
        return target in refs
    return bool(sources)


def _write_subscription_terminal_event(result: dict[str, Any]) -> None:
    outcome = str(result.get("outcome") or ("success" if result.get("ok") else "failed"))
    event_type = {
        "partial": "subscription_refresh_partial",
        "failed": "subscription_refresh_failed",
        "unconfirmed": "subscription_refresh_unconfirmed",
        "no_op": "subscription_refresh_skipped",
        "success": "subscription_refresh_applied",
    }.get(outcome, "subscription_refresh_failed")
    message = {
        "partial": "Subscription refresh completed with source errors; runtime outcome is verified.",
        "failed": "Subscription refresh failed before a verified runtime transition.",
        "unconfirmed": "Subscription refresh runtime outcome could not be verified.",
        "no_op": "Subscription refresh was verified with no runtime changes.",
        "success": "Subscription refresh completed after runtime verification.",
    }[outcome]
    write_operational_log(
        event_type=event_type,
        level="warning" if outcome == "partial" else "error" if outcome in {"failed", "unconfirmed"} else "info",
        message=message,
        details={
            "operation": "subscription_refresh",
            "outcome": outcome,
            "runtime_verified": bool(result.get("runtime_verified")),
            "intent_saved": bool(result.get("intent_saved")),
            "last_good_retained": bool(result.get("last_good_retained")),
            "stage": result.get("stage"),
            "error_code": (result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None,
            "error_message": redact_subscription_public_value((result.get("error") or {}).get("message")) if isinstance(result.get("error"), dict) else None,
            "source_outcomes": result.get("source_outcomes") or [],
        },
    )


def _failed_before_apply(prepared: dict[str, Any]) -> dict[str, Any]:
    refresh = prepared.get("refresh") if isinstance(prepared.get("refresh"), dict) else {}
    result = {
        **prepared,
        "ok": False,
        "outcome": "failed",
        "runtime_verified": False,
        "intent_saved": _subscription_intent_saved(refresh),
        "last_good_retained": False,
        "source_outcomes": _source_outcomes(refresh),
    }
    _write_subscription_terminal_event(result)
    return result


def validate_mihomo_candidate_config(candidate_path: str | None = None) -> dict[str, Any]:
    """Validate current Mihomo candidate config with Mihomo docker image."""
    resolved_candidate_path = str(candidate_path or MIHOMO_CANDIDATE_CONFIG_PATH)

    try:
        validation = subprocess.run(
            [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "-v",
            f"{resolved_candidate_path}:/config/config.yaml:ro",
            MIHOMO_IMAGE,
            "-t",
            "-f",
            "/config/config.yaml",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": str(exc)[-1000:],
            "error_code": "MIHOMO_NATIVE_VALIDATOR_UNAVAILABLE",
        }

    return {
        "ok": validation.returncode == 0,
        "returncode": validation.returncode,
        "stdout_tail": validation.stdout[-1000:],
        "stderr_tail": validation.stderr[-1000:],
    }


def prepare_subscription_refresh(*, source_ref: str | None = None) -> dict[str, Any]:
    """Run staged subscription refresh without applying runtime changes.

    Pipeline:
    1. refresh provider subscription;
    2. sync server inventory into SQLite;
    3. generate Mihomo candidate config;
    4. validate candidate config;
    5. do not promote active config;
    6. do not restart Mihomo container.
    """

    started_at = perf_counter()
    refresh_started_at = perf_counter()
    refresh_result = (
        refresh_one_subscription_inventory(source_ref)
        if source_ref is not None
        else refresh_all_subscription_inventory()
    )
    inventory_refresh_ms = round((perf_counter() - refresh_started_at) * 1000, 2)

    if not refresh_result["ok"]:
        return {
            "ok": False,
            "stage": refresh_result.get("stage"),
            "refresh": refresh_result,
            "candidate": None,
            "config_validation": None,
            "promoted": False,
            "container_restarted": False,
            "error": refresh_result.get("error"),
            "timings_ms": {
                "inventory_refresh": inventory_refresh_ms,
                "prepare_total": round((perf_counter() - started_at) * 1000, 2),
            },
        }

    input_fingerprint = current_mihomo_input_fingerprint()
    if mihomo_input_unchanged(input_fingerprint):
        return {
            "ok": True,
            "stage": "already_current",
            "refresh": refresh_result,
            "candidate": {"skipped": True, "reason": "input_fingerprint_unchanged"},
            "config_validation": {"ok": True, "skipped": True, "reason": "input_fingerprint_unchanged"},
            "prepared_candidate_metadata": None,
            "promoted": False,
            "container_restarted": False,
            "error": None,
            "timings_ms": {
                "inventory_refresh": inventory_refresh_ms,
                "prepare_total": round((perf_counter() - started_at) * 1000, 2),
            },
        }

    candidate_started_at = perf_counter()
    candidate_internal = _write_generated_candidate()
    config_validation = _validate_generated_candidate(candidate_internal)
    candidate = _summarize_candidate(candidate_internal)
    candidate_prepare_ms = round((perf_counter() - candidate_started_at) * 1000, 2)

    if not config_validation["ok"]:
        return {
            "ok": False,
            "stage": "config_validation",
            "refresh": refresh_result,
            "candidate": candidate,
            "config_validation": config_validation,
            "prepared_candidate_metadata": None,
            "promoted": False,
            "container_restarted": False,
            "error": {
                "code": "MIHOMO_CONFIG_VALIDATION_FAILED",
                "message": "Generated Mihomo candidate config failed validation.",
            },
            "timings_ms": {
                "inventory_refresh": inventory_refresh_ms,
                "candidate_prepare_validation": candidate_prepare_ms,
                "prepare_total": round((perf_counter() - started_at) * 1000, 2),
            },
        }

    return {
        "ok": True,
        "stage": "candidate_validated",
        "refresh": refresh_result,
        "candidate": candidate,
        "config_validation": config_validation,
        "prepared_candidate_metadata": {
            "input_fingerprint_hash": input_fingerprint.get("hash"),
            "input_fingerprint_version": input_fingerprint.get("version"),
            "candidate_file_hash": _file_hash(candidate.get("candidate_path") or MIHOMO_CANDIDATE_CONFIG_PATH),
            "docker_validation_ok": bool(config_validation.get("ok")),
            "_candidate_config": candidate_internal.get("_candidate_config"),
        },
        "promoted": False,
        "container_restarted": False,
        "error": None,
        "timings_ms": {
            "inventory_refresh": inventory_refresh_ms,
            "candidate_prepare_validation": candidate_prepare_ms,
            "prepare_total": round((perf_counter() - started_at) * 1000, 2),
        },
    }


def _apply_prepared_subscription_refresh_under_xray_guard(prepared: dict[str, Any]) -> dict[str, Any]:
    """Reconcile Mihomo runtime after a prepared subscription inventory refresh."""

    started_at = perf_counter()
    public_prepared = _public_prepared_result(prepared)
    from fwrouter_api.services.xray_runtime_state import _module_state
    xray_module = _module_state("xray") or {}
    staged_xray_first = (
        str(xray_module.get("desired_state") or "") == "enabled"
        and str(xray_module.get("lifecycle_mode") or "") == "managed"
    )
    transition_preflight = _subscription_transition_preflight()
    if not transition_preflight.get("ok"):
        failed = {
            **public_prepared,
            "ok": False,
            "stage": "preflight",
            "outcome": "failed",
            "runtime_verified": False,
            "intent_saved": bool(prepared.get("refresh", {}).get("state")),
            "last_good_retained": False,
            "source_outcomes": _source_outcomes(prepared.get("refresh") or {}),
            "promoted": False,
            "container_restarted": False,
            "applied": False,
            "error": transition_preflight.get("error"),
        }
        _write_subscription_terminal_event(failed)
        return failed
    xray_vpn_auto_reconcile: dict[str, Any] | None = None
    xray_profile_reconcile: dict[str, Any] | None = None
    xray_reconcile_ms: float | None = None
    nonstaged_selection: dict[str, Any] = {}
    selection_before: dict[str, Any] | None = None
    selection_after: dict[str, Any] | None = None

    def verify_nonstaged_selection() -> dict[str, Any]:
        nonlocal nonstaged_selection, selection_before, selection_after
        from fwrouter_api.services.xray_subscription_service import _capture_generation_auto_selection
        selection_before = _capture_generation_auto_selection()
        try:
            nonstaged_selection = _maybe_select_vpn_auto_after_refresh()
        finally:
            selection_after = _capture_generation_auto_selection()
        if nonstaged_selection.get("ok", True):
            from fwrouter_api.services.provider_managed import verify_provider_handoff
            verification = verify_provider_handoff(prepared)
            if not verification.get("ok"):
                nonstaged_selection = verification
        return nonstaged_selection

    def restore_nonstaged_selection() -> bool:
        if not selection_before or not selection_after:
            return False
        from fwrouter_api.db.connection import db_session
        from fwrouter_api.services.xray_subscription_service import _restore_generation_auto_selection
        with db_session() as connection:
            return _restore_generation_auto_selection(
                connection, before=selection_before, after=selection_after,
            )

    if staged_xray_first:
        xray_started_at = perf_counter()
        prepublication_selection: dict[str, Any] = {}

        def verify_selection_before_publication() -> dict[str, Any]:
            nonlocal prepublication_selection
            prepublication_selection = _maybe_select_vpn_auto_after_refresh()
            if prepublication_selection.get("ok", True):
                from fwrouter_api.services.provider_managed import verify_provider_handoff
                verification = verify_provider_handoff(prepared)
                if not verification.get("ok"):
                    prepublication_selection = verification
            return prepublication_selection

        xray_vpn_auto_reconcile, xray_profile_reconcile = (
            _reconcile_xray_after_authoritative_inventory_refresh(
                prepared,
                verification_callback=verify_selection_before_publication,
            )
        )
        xray_reconcile_ms = round((perf_counter() - xray_started_at) * 1000, 2)
        if not bool(xray_vpn_auto_reconcile.get("ok", True)) or not bool(xray_profile_reconcile.get("ok", True)):
            reconcile = {
                "ok": False,
                "stage": "xray_generation",
                "error_code": xray_profile_reconcile.get("error_code") or xray_vpn_auto_reconcile.get("error_code"),
                "error_message": xray_profile_reconcile.get("error_message") or xray_vpn_auto_reconcile.get("error_message"),
            }
        else:
            # The staged generation has already applied and read back the exact
            # validated candidates. Do not open a second uncheckpointed apply.
            reconcile = {
                "ok": True,
                "reconcile_action": "staged_generation_verified",
                "reconcile_reason": "xray_generation_committed",
                "promoted": {"promoted": False, "error_code": None},
                "container": {"action": "none", "ok": True},
            }
    else:
        reconcile = None
    initial_reconcile_started_at = perf_counter()
    if reconcile is None:
        prepared_metadata = prepared.get("prepared_candidate_metadata")
        reconcile = (
            reconcile_mihomo_runtime(prepared_candidate_metadata=prepared_metadata, verification_callback=verify_nonstaged_selection)
            if prepared_metadata
            else reconcile_mihomo_runtime(verification_callback=verify_nonstaged_selection)
        )
    initial_reconcile_ms = round((perf_counter() - initial_reconcile_started_at) * 1000, 2)
    mihomo_recovery_checkpoint = reconcile.pop("_recovery_checkpoint", None)
    promoted = bool((reconcile.get("promoted") or {}).get("promoted"))
    container_action = str((reconcile.get("container") or {}).get("action") or "none")
    container_restarted = container_action not in {"", "none"}
    reconcile_reason = str(reconcile.get("reconcile_reason") or "")
    reconcile_action = str(reconcile.get("reconcile_action") or "none")

    if reconcile.get("ok"):
        selector_started_at = perf_counter()
        auto_select = (
            prepublication_selection
            if staged_xray_first and prepublication_selection
            else nonstaged_selection
            if nonstaged_selection
            else _maybe_select_vpn_auto_after_refresh()
        )
        selector_ms = round((perf_counter() - selector_started_at) * 1000, 2)
        if xray_vpn_auto_reconcile is None or xray_profile_reconcile is None:
            xray_started_at = perf_counter()
            xray_vpn_auto_reconcile, xray_profile_reconcile = (
                _reconcile_xray_after_authoritative_inventory_refresh(prepared)
            )
            xray_reconcile_ms = round((perf_counter() - xray_started_at) * 1000, 2)
        xray_ok = bool(xray_vpn_auto_reconcile.get("ok", True)) and bool(
            xray_profile_reconcile.get("ok", True)
        )
        final_reconcile: dict[str, Any] | None = None
        public_profile_promote: dict[str, Any] | None = None
        if xray_ok and not staged_xray_first and str(xray_profile_reconcile.get("status") or "") == "success":
            # The first Mihomo pass intentionally retains last-good Xray
            # handoffs. Once Xray has converged, regenerate from its applied
            # binding state to remove obsolete listeners before publishing the
            # new public profile.
            final_reconcile_started_at = perf_counter()
            final_reconcile = reconcile_mihomo_runtime()
            final_reconcile_ms = round((perf_counter() - final_reconcile_started_at) * 1000, 2)
            xray_ok = bool(final_reconcile.get("ok"))
            if xray_ok:
                from fwrouter_api.services.subscription_profiles import (
                    list_desired_subscription_xray_clients,
                    promote_runtime_verified_subscription_nodes,
                )

                public_profile_promote = promote_runtime_verified_subscription_nodes(
                    list_desired_subscription_xray_clients()
                )
        else:
            final_reconcile_ms = None
        generation_apply = xray_profile_reconcile.get("generation_apply") if isinstance(xray_profile_reconcile.get("generation_apply"), dict) else {}
        generation_changed = any(
            str((generation_apply.get(key) or {}).get("stage") or "") == "applied"
            for key in ("transition_mihomo", "xray", "final_mihomo")
        ) or bool(generation_apply.get("public_snapshots_changed"))
        if staged_xray_first and public_profile_promote is None:
            public_profile_promote = xray_profile_reconcile.get("public_profile_promote")
        if staged_xray_first:
            promoted = promoted or generation_changed
            generation_mihomo_restarted = any(
                str(
                    ((generation_apply.get(key) or {}).get("container") or {}).get("action")
                    or "none"
                )
                not in {"", "none"}
                for key in ("transition_mihomo", "final_mihomo")
            )
            container_restarted = container_restarted or generation_mihomo_restarted
        timings_ms = {
            "initial_runtime_reconcile": initial_reconcile_ms,
            "selector": selector_ms,
            "xray_reconcile_materialization": xray_reconcile_ms,
            "final_mihomo_reconcile": final_reconcile_ms,
            "apply_total": round((perf_counter() - started_at) * 1000, 2),
        }
        selector_result = auto_select.get("selector") if isinstance(auto_select.get("selector"), dict) else {}
        auto_transition = selector_result.get("auto_transition") if isinstance(selector_result.get("auto_transition"), dict) else {}
        effective_route = selector_result.get("effective_route") if isinstance(selector_result.get("effective_route"), dict) else {}
        selection_changed = bool(
            auto_transition.get("changed")
            or effective_route.get("changed")
            or selector_result.get("changed")
        )
        runtime_changed = promoted or container_restarted or generation_changed or selection_changed
        result = {
            **public_prepared,
            "ok": bool(auto_select.get("ok", True)) and xray_ok,
            "stage": (
                "verify"
                if not xray_ok
                else "applied" if runtime_changed else "already_current"
            ),
            "outcome": (
                "unconfirmed" if reconcile.get("ok") and not (auto_select.get("ok", True) and xray_ok)
                else "failed" if not reconcile.get("ok")
                else "partial" if int(((prepared.get("refresh") or {}).get("batch") or {}).get("errors") or 0) > 0
                else "no_op" if not runtime_changed
                else "success"
            ),
            "runtime_verified": bool(reconcile.get("ok") and auto_select.get("ok", True) and xray_ok),
            "intent_saved": _subscription_intent_saved(prepared.get("refresh") or {}),
            "last_good_retained": (
                any(item.get("retained") for item in _source_outcomes(prepared.get("refresh") or {}))
                or (not runtime_changed and bool(reconcile.get("ok") and auto_select.get("ok", True) and xray_ok))
            ),
            "source_outcomes": _source_outcomes(prepared.get("refresh") or {}),
            "reconcile": reconcile,
            "promoted": promoted,
            "container_restarted": container_restarted,
            "applied": runtime_changed,
            "update_available": runtime_changed,
            "reconcile_action": reconcile_action,
            "reconcile_reason": reconcile_reason,
            "auto_select": auto_select,
            "xray_vpn_auto_reconcile": xray_vpn_auto_reconcile,
            "xray_profile_reconcile": xray_profile_reconcile,
            "final_mihomo_reconcile": final_reconcile,
            "public_profile_promote": public_profile_promote,
            "timings_ms": {**(prepared.get("timings_ms") or {}), **timings_ms},
            "error": (
                None
                if auto_select.get("ok", True) and xray_ok
                else {
                    "code": (
                        "VPN_AUTO_AUTOSELECT_FAILED"
                        if not auto_select.get("ok", True)
                        else (
                            (final_reconcile or {}).get("promoted", {}).get("error_code")
                            or (final_reconcile or {}).get("container", {}).get("error_code")
                            or xray_vpn_auto_reconcile.get("error_code")
                            or xray_profile_reconcile.get("error_code")
                        )
                        or "XRAY_SUBSCRIPTION_PROFILE_RECONCILE_FAILED"
                    ),
                    "message": (
                        "Subscription refresh completed, but vpn-auto could not select a valid active server."
                        if not auto_select.get("ok", True)
                        else (
                            (final_reconcile or {}).get("promoted", {}).get("error_message")
                            or (final_reconcile or {}).get("container", {}).get("error_message")
                            or xray_vpn_auto_reconcile.get("error_message")
                            or xray_profile_reconcile.get("error_message")
                            or "Subscription refresh completed, but Xray public profile runtime did not converge."
                        )
                    ),
                }
            ),
        }
        if result.get("runtime_verified"):
            from fwrouter_api.services.provider_managed import record_provider_applied
            record_provider_applied(prepared)
        event_type = ("subscription_refresh_partial" if result["outcome"] == "partial" else "subscription_refresh_applied" if result["applied"] else "subscription_refresh_skipped")
        message = (
            "Subscription refresh completed with provider errors; retained last-good source inventory was applied and runtime verified."
            if result["outcome"] == "partial"
            else "Subscription refresh downloaded new data and reconciled Mihomo runtime."
            if result["applied"]
            else "Subscription refresh completed with no runtime changes after verification."
        )
        details = {
            "stage": result["stage"],
            "outcome": result["outcome"],
            "runtime_verified": result["runtime_verified"],
            "intent_saved": result["intent_saved"],
            "last_good_retained": result["last_good_retained"],
            "applied": result["applied"],
            "promoted": result["promoted"],
            "container_restarted": result["container_restarted"],
            "reconcile_action": reconcile_action,
            "reconcile_reason": reconcile_reason,
            "auto_select": auto_select.get("status"),
            "xray_profile_reconcile": {
                "status": xray_profile_reconcile.get("status"),
                "nodes_count": xray_profile_reconcile.get("nodes_count"),
                "created_count": xray_profile_reconcile.get("created_count"),
                "deleted_count": xray_profile_reconcile.get("deleted_count"),
                "error_code": xray_profile_reconcile.get("error_code"),
            },
            "xray_vpn_auto_reconcile": {
                "status": xray_vpn_auto_reconcile.get("status"),
                "created_count": xray_vpn_auto_reconcile.get("created_count"),
                "deleted_count": xray_vpn_auto_reconcile.get("deleted_count"),
                "error_code": xray_vpn_auto_reconcile.get("error_code"),
            },
            "final_mihomo_reconcile": {
                "ok": (final_reconcile or {}).get("ok"),
                "reconcile_reason": (final_reconcile or {}).get("reconcile_reason"),
            },
            "timings_ms": result.get("timings_ms"),
        }
        _write_subscription_terminal_event(result)
        write_technical_log(
            component="subscription",
            event_type=event_type,
            level="warning" if result["outcome"] == "partial" else "info" if result["ok"] else "error",
            message=message,
            details={
                "stage": result["stage"],
                "applied": result["applied"],
                "reconcile_action": reconcile_action,
                "reconcile_reason": reconcile_reason,
                "error": result.get("error"),
                "timings_ms": result.get("timings_ms"),
            },
        )
        return result

    error_code = str(
        reconcile.get("error_code")
        or ((reconcile.get("verification_callback_result") or {}).get("error_code") if isinstance(reconcile.get("verification_callback_result"), dict) else None)
        or (reconcile.get("promoted") or {}).get("error_code")
        or (reconcile.get("container") or {}).get("error_code")
        or "SUBSCRIPTION_RUNTIME_RECONCILE_FAILED"
    )
    error_message = str(
        reconcile.get("error_message")
        or ((reconcile.get("verification_callback_result") or {}).get("error_message") if isinstance(reconcile.get("verification_callback_result"), dict) else None)
        or (reconcile.get("promoted") or {}).get("error_message")
        or (reconcile.get("container") or {}).get("error_message")
        or "Subscription refresh failed while applying Mihomo runtime changes."
    )
    late_recovery = None
    if not staged_xray_first:
        generation_recovery = reconcile.get("generation_recovery")
        if isinstance(generation_recovery, dict):
            late_recovery = dict(generation_recovery)
        elif mihomo_recovery_checkpoint:
            from fwrouter_api.services.mihomo_reconcile import restore_mihomo_reconcile_checkpoint
            late_recovery = restore_mihomo_reconcile_checkpoint(mihomo_recovery_checkpoint)
        if selection_before and selection_after:
            selection_restored = restore_nonstaged_selection()
            from fwrouter_api.services.xray_subscription_service import _verify_generation_selection_readback
            selection_readback = _verify_generation_selection_readback(selection_before) if selection_restored else {"ok": False}
            if late_recovery is None:
                late_recovery = {"ok": bool(selection_restored and selection_readback.get("ok")), "runtime_unchanged": True}
            else:
                late_recovery["ok"] = bool(late_recovery.get("ok") and selection_restored and selection_readback.get("ok"))
            late_recovery["selection_restored"] = selection_restored
            late_recovery["selection_readback"] = selection_readback
    result = {
        **public_prepared,
        "ok": False,
        "outcome": "unconfirmed" if (late_recovery is not None and not late_recovery.get("ok")) or (isinstance(reconcile.get("verification_callback_result"), dict) and not reconcile["verification_callback_result"].get("ok") and not reconcile.get("last_good_retained")) else "failed",
        "runtime_verified": False,
        "intent_saved": _subscription_intent_saved(prepared.get("refresh") or {}),
        "last_good_retained": bool(late_recovery.get("ok")) if late_recovery is not None else bool(reconcile.get("last_good_retained")) or bool((xray_profile_reconcile or {}).get("last_good_retained")),
        "source_outcomes": _source_outcomes(prepared.get("refresh") or {}),
        "stage": "xray_generation" if reconcile.get("stage") == "xray_generation" else "apply_runtime",
        "reconcile": reconcile,
        "promoted": promoted,
        "container_restarted": container_restarted,
        "applied": False,
        "update_available": True,
        "reconcile_action": reconcile_action,
        "reconcile_reason": reconcile_reason,
        "error": {
            "code": error_code,
            "message": error_message,
        },
        "timings_ms": {
            **(prepared.get("timings_ms") or {}),
            "initial_runtime_reconcile": initial_reconcile_ms,
            "apply_total": round((perf_counter() - started_at) * 1000, 2),
        },
    }
    _write_subscription_terminal_event(result)
    write_technical_log(
        component="subscription",
        event_type="subscription_refresh_apply_failed",
        level="warning",
        message="Subscription refresh failed while reconciling Mihomo runtime.",
        details={
            "stage": result["stage"],
            "error": result["error"],
            "reconcile_action": reconcile_action,
            "reconcile_reason": reconcile_reason,
            "timings_ms": result.get("timings_ms"),
        },
    )
    return result


def apply_prepared_subscription_refresh(prepared: dict[str, Any]) -> dict[str, Any]:
    """Authoritative refresh bypasses the quiet window and completes under Xray writer guard."""

    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.xray_vpn_auto_pending import (
        clear_pending_revision_after_success,
        get_xray_vpn_auto_pending_state,
    )

    with xray_writer_guard():
        pending = get_xray_vpn_auto_pending_state()
        revision = int(pending.get("revision") or 0) if pending.get("pending") else None
        result = _apply_prepared_subscription_refresh_under_xray_guard(prepared)
        full_success = bool(
            revision is not None
            and result.get("ok")
            and isinstance(result.get("xray_vpn_auto_reconcile"), dict)
            and result["xray_vpn_auto_reconcile"].get("status") == "success"
            and isinstance(result.get("xray_profile_reconcile"), dict)
            and result["xray_profile_reconcile"].get("ok")
            and isinstance(result.get("final_mihomo_reconcile"), dict)
            and result["final_mihomo_reconcile"].get("ok")
            and isinstance(result.get("public_profile_promote"), dict)
        )
        if full_success:
            result["pending_revision_cleared"] = clear_pending_revision_after_success(revision)
        return result


def _reconcile_xray_subscription_profiles_after_refresh(
    *,
    promote_public_profile: bool = True,
    verification_callback: Any = None,
) -> dict[str, Any]:
    """Keep public VLESS profile identities converged after server inventory changes."""

    try:
        from fwrouter_api.services.xray_runtime_state import _module_state
        from fwrouter_api.services.xray_subscription_service import (
            reconcile_xray_subscription_profile_nodes,
        )

        module = _module_state("xray") or {}
        if str(module.get("desired_state") or "") != "enabled":
            return {
                "ok": True,
                "status": "skipped",
                "reason": "xray_module_disabled",
                "nodes_count": 0,
            }
        return reconcile_xray_subscription_profile_nodes(
            requested_by="subscription-refresh",
            promote_public_profile=promote_public_profile,
            verification_callback=verification_callback,
        )
    except Exception as exc:  # pragma: no cover - defensive runtime path
        return {
            "ok": False,
            "status": "failed",
            "stage": "xray_profile_reconcile",
            "error_code": "XRAY_SUBSCRIPTION_PROFILE_RECONCILE_EXCEPTION",
            "error_message": str(exc),
        }


def _reconcile_xray_after_authoritative_inventory_refresh(
    prepared: dict[str, Any],
    *,
    verification_callback: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconcile generated vpn-auto identities only after a successful refresh.

    The first-pass inventory refresh can be partial: failed providers retain
    their last-good inventory. The refresh result's ``ok`` flag means at least
    one provider completed successfully, while all-provider failures return
    false and must never drive generated-client pruning.
    """

    refresh = prepared.get("refresh") if isinstance(prepared.get("refresh"), dict) else {}
    if prepared.get("ok") is not True or refresh.get("ok") is not True:
        skipped = {
            "ok": True,
            "status": "skipped",
            "reason": "inventory_refresh_not_authoritative",
            "created_count": 0,
            "deleted_count": 0,
        }
        return skipped, _reconcile_xray_subscription_profiles_after_refresh(
            promote_public_profile=True
        )

    try:
        from fwrouter_api.services.xray_runtime_state import _module_state
        from fwrouter_api.services.xray_subscription_service import (
            reconcile_xray_vpn_auto_subscription,
        )

        module = _module_state("xray") or {}
        if (
            str(module.get("desired_state") or "") != "enabled"
            or str(module.get("lifecycle_mode") or "") != "managed"
        ):
            skipped = {
                "ok": True,
                "status": "skipped",
                "reason": "managed_xray_runtime_unavailable",
                "created_count": 0,
                "deleted_count": 0,
            }
            return skipped, _reconcile_xray_subscription_profiles_after_refresh(
                promote_public_profile=True
            )

        auto_reconcile = reconcile_xray_vpn_auto_subscription(
            requested_by="subscription-refresh",
            verification_callback=verification_callback,
        )
        profile_reconcile = auto_reconcile.get("profile_reconcile")
        if not isinstance(profile_reconcile, dict):
            profile_reconcile = {
                "ok": bool(auto_reconcile.get("ok")),
                "status": str(auto_reconcile.get("status") or "failed"),
                "error_code": auto_reconcile.get("error_code"),
                "error_message": auto_reconcile.get("error_message"),
                "nodes_count": int(auto_reconcile.get("nodes_count") or 0),
            }
        auto_summary = {
            "ok": bool(auto_reconcile.get("ok")),
            "status": str(auto_reconcile.get("status") or "unknown"),
            "created_count": int(auto_reconcile.get("created_count") or 0),
            "deleted_count": int(auto_reconcile.get("deleted_count") or 0),
            "nodes_count": int(auto_reconcile.get("nodes_count") or 0),
            "error_code": auto_reconcile.get("error_code"),
            "stage": auto_reconcile.get("stage"),
        }
        return auto_summary, profile_reconcile
    except Exception as exc:  # pragma: no cover - defensive runtime path
        failed = {
            "ok": False,
            "status": "failed",
            "stage": "vpn_auto_reconcile",
            "error_code": "XRAY_VPN_AUTO_RECONCILE_EXCEPTION",
            "error_message": str(exc),
        }
        return failed, failed


def apply_subscription_import_result(refresh_result: dict[str, Any]) -> dict[str, Any]:
    """Generate, validate and apply Mihomo runtime after an already synced import."""

    if not refresh_result.get("ok"):
        return _failed_before_apply({
            "ok": False,
            "stage": refresh_result.get("stage"),
            "refresh": refresh_result,
            "candidate": None,
            "config_validation": None,
            "promoted": False,
            "container_restarted": False,
            "error": refresh_result.get("error"),
        })

    candidate_internal = _write_generated_candidate()
    config_validation = _validate_generated_candidate(candidate_internal)
    candidate = _summarize_candidate(candidate_internal)
    import_fingerprint = current_mihomo_input_fingerprint()
    prepared_metadata = {
        "input_fingerprint_hash": import_fingerprint.get("hash"),
        "input_fingerprint_version": import_fingerprint.get("version"),
        "candidate_file_hash": _file_hash(candidate.get("candidate_path") or MIHOMO_CANDIDATE_CONFIG_PATH),
        "docker_validation_ok": bool(config_validation.get("ok")),
        "_candidate_config": candidate_internal.get("_candidate_config"),
    }

    if not config_validation["ok"]:
        return _failed_before_apply({
            "ok": False,
            "stage": "config_validation",
            "refresh": refresh_result,
            "candidate": candidate,
            "config_validation": config_validation,
            "promoted": False,
            "container_restarted": False,
            "error": {
                "code": "MIHOMO_CONFIG_VALIDATION_FAILED",
                "message": "Generated Mihomo candidate config failed validation.",
            },
        })

    return apply_prepared_subscription_refresh({
        "ok": True,
        "stage": "candidate_validated",
        "refresh": refresh_result,
        "candidate": candidate,
        "config_validation": config_validation,
        "prepared_candidate_metadata": prepared_metadata,
        "promoted": False,
        "container_restarted": False,
        "error": None,
    })


def refresh_all_subscriptions() -> dict[str, Any]:
    """Canonical full refresh: fetch all saved sources and reconcile once."""
    return apply_subscription_refresh()


def refresh_subscription(source_ref: str) -> dict[str, Any]:
    """Canonical targeted refresh, serialized and applied through the same pipeline."""
    return apply_subscription_refresh(source_ref=source_ref)


def apply_subscription_refresh(*, source_ref: str | None = None) -> dict[str, Any]:
    """Refresh subscription inventory and reconcile Mihomo runtime if changed.

    Pipeline:
    1. refresh provider subscription;
    2. sync server inventory into SQLite;
    3. generate Mihomo candidate config;
    4. validate candidate config;
    5. compare candidate with active config;
    6. promote/restart Mihomo only when candidate differs from active config.
    """

    from fwrouter_api.adapters.xray_common import xray_writer_guard

    # Hold the shared writer guard across fetch, persistent inventory update,
    # and verified apply. The nested apply guard is re-entrant.
    with xray_writer_guard():
        prepared = (
            prepare_subscription_refresh(source_ref=source_ref)
            if source_ref is not None
            else prepare_subscription_refresh()
        )
        if not prepared.get("ok"):
            return _failed_before_apply(prepared)

        return apply_prepared_subscription_refresh(prepared)
