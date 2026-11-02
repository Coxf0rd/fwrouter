"""Read-only Xray generation and native-state observations.

The coordinator remains the owner of operation state and publication. These
functions preserve their existing database session boundaries and return
observations only; native proof helpers may refresh the existing short-lived
readback cache entries as part of their established contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services import xray_generation_fingerprint as _xray_generation_fingerprint
from fwrouter_api.services.xray_common import _xray_adapter

FIXED_ROUTING_FIELDS = (
    "desired_mode", "applied_mode", "selective_default", "server_mode",
    "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until", "apply_state",
)
_FIXED_XRAY_ROUTING_FIELDS = FIXED_ROUTING_FIELDS

__all__ = [
    "FIXED_ROUTING_FIELDS",
    "capture_generation_selection",
    "capture_ordinary_fixed_context",
    "collect_core_native_proof",
    "fixed_generation_context_matches",
    "fixed_routing_projection",
    "fixed_routing_snapshot_matches",
    "generation_source_fingerprint",
    "legacy_admission_source_fingerprint",
    "legacy_core_runtime_fingerprint",
    "legacy_core_runtime_snapshot",
    "legacy_core_unavailable_reason",
    "legacy_runtime_fence_facts",
    "legacy_runtime_fences_match",
    "ordinary_fixed_checkpoint_context_matches",
    "ordinary_fixed_context_matches",
    "verify_generation_selection",
]

def generation_source_fingerprint(connection: sqlite3.Connection | None = None) -> str:
    if connection is None:
        with db_session() as active:
            sources = _xray_generation_fingerprint.generation_source_projection(active)
        return _xray_generation_fingerprint.source_projection_sha256(sources)
    return _xray_generation_fingerprint.generation_source_fingerprint(connection)

def legacy_admission_source_fingerprint(connection: sqlite3.Connection | None = None) -> str:
    """Proof-only extension of the historical generation source fence."""
    if connection is None:
        with db_session() as active:
            values = _xray_generation_fingerprint.legacy_admission_source_projection(
                active, generation_fingerprint=_generation_source_fingerprint,
            )
        return _xray_generation_fingerprint.source_projection_sha256(values)
    return _xray_generation_fingerprint.legacy_admission_source_fingerprint(
        connection, generation_fingerprint=_generation_source_fingerprint,
    )

def legacy_core_runtime_snapshot(connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Capture Core/module apply observations for one fresh proof attempt only."""
    if connection is None:
        with db_session() as active:
            return _xray_generation_fingerprint.legacy_admission_core_runtime_snapshot(active)
    return _xray_generation_fingerprint.legacy_admission_core_runtime_snapshot(connection)

def legacy_core_runtime_fingerprint(connection: sqlite3.Connection | None = None) -> str:
    snapshot = _legacy_admission_core_runtime_snapshot(connection)
    return _xray_generation_fingerprint.legacy_admission_core_runtime_fingerprint(snapshot)

def legacy_core_unavailable_reason(connection: sqlite3.Connection | None = None) -> str | None:
    """Reject bypass, incomplete Core apply state, or an active apply owner."""
    if connection is None:
        with db_session() as active:
            return _legacy_admission_core_unavailable_reason(active)
    bypass_row = connection.execute(
        "SELECT value_json FROM settings WHERE key='core.bypass'",
    ).fetchone()
    if bypass_row is not None:
        try:
            bypass = json.loads(str(bypass_row["value_json"] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return "core_bypass_state_unverifiable"
        if not isinstance(bypass, dict):
            return "core_bypass_state_unverifiable"
        if type(bypass.get("enabled", False)) is not bool:
            return "core_bypass_state_unverifiable"
        if bypass.get("enabled"):
            return "core_bypass_active"

    core = connection.execute(
        "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
    ).fetchone()
    routing = connection.execute(
        "SELECT desired_mode, applied_mode, apply_state FROM routing_global_state WHERE id=1",
    ).fetchone()
    latest_apply = connection.execute(
        "SELECT apply_id, status, core_intent_revision FROM apply_versions ORDER BY created_at DESC, apply_id DESC LIMIT 1",
    ).fetchone()
    if latest_apply is None or latest_apply["status"] != "applied":
        return "core_applied_generation_unowned"
    from fwrouter_api.services.core_intent import read_core_intent_revision
    try:
        current_revision = read_core_intent_revision(connection)
    except ValueError:
        return "core_intent_revision_unverifiable"
    if latest_apply["core_intent_revision"] != current_revision:
        return "core_applied_generation_unowned"
    if (
        core is None or core["desired_state"] != "enabled" or core["lifecycle_mode"] != "managed"
        or core["apply_state"] != "clean" or routing is None
        or routing["apply_state"] != "clean" or routing["desired_mode"] != routing["applied_mode"]
    ):
        return "core_apply_state_unverified"

    # Use the existing lock-key tokenization without its stale-job cleanup side
    # effect. Admission must remain a read-only observer of active owners.
    from fwrouter_api.services.jobs import _lock_tokens
    active_rows = connection.execute(
        "SELECT job_type, lock_key FROM jobs WHERE status IN ('queued', 'running')",
    ).fetchall()
    blocked_tokens = {"apply", "module:core", "selector", "xray"}
    for row in active_rows:
        if row["job_type"] == "core_bypass" or blocked_tokens.intersection(_lock_tokens(row["lock_key"])):
            return "core_apply_inflight"
    return None

def collect_core_native_proof() -> dict[str, Any]:
    """Prove the currently-owned Core apply and its live enforcement mode.

    This is a read-only proof collector. It refreshes only existing short-lived
    readback cache keys and never invokes the Core apply owner.
    """
    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status
    from fwrouter_api.services.live_probe_cache import get_live_probe_cache
    from fwrouter_api.services.core_intent import read_core_runtime_context

    try:
        runtime_context_before = read_core_runtime_context()
        if runtime_context_before is None:
            return {"ok": False, "reason": "core_runtime_context_unavailable"}
        manifest = get_live_probe_cache(
            "dataplane_global.applied_manifest", ttl_seconds=5.0,
            loader=dataplane_global._read_applied_manifest_uncached, force_refresh=True,
        )
        if not isinstance(manifest, dict) or not isinstance(manifest.get("plan_id"), str):
            return {"ok": False, "reason": "core_applied_manifest_unavailable"}
        manifest_apply_id = manifest["plan_id"]
        if not manifest_apply_id or (
            "apply_id" in manifest and manifest.get("apply_id") != manifest_apply_id
        ):
            return {"ok": False, "reason": "core_applied_manifest_unavailable"}
        with db_session() as connection:
            routing_row = connection.execute(
                "SELECT desired_mode, applied_mode, selective_default, server_mode, "
                "desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
                "active_auto_server_id, apply_state FROM routing_global_state WHERE id=1",
            ).fetchone()
            owned_apply = connection.execute(
                "SELECT status, core_intent_revision FROM apply_versions WHERE apply_id=?", (manifest_apply_id,),
            ).fetchone()
            latest_apply = connection.execute(
                "SELECT apply_id, status, core_intent_revision FROM apply_versions ORDER BY created_at DESC, apply_id DESC LIMIT 1",
            ).fetchone()
            from fwrouter_api.services.core_intent import read_core_intent_revision
            try:
                core_revision = read_core_intent_revision(connection)
            except ValueError:
                return {"ok": False, "reason": "core_intent_revision_unverifiable"}
            routing = dict(routing_row) if routing_row is not None else None
        if routing is None or routing["apply_state"] != "clean":
            return {"ok": False, "reason": "core_routing_state_unverified"}
        if (
            owned_apply is None or owned_apply["status"] != "applied"
            or latest_apply is None or latest_apply["status"] != "applied"
            or str(latest_apply["apply_id"]) != manifest_apply_id
            or latest_apply["core_intent_revision"] != core_revision
            or owned_apply["core_intent_revision"] != core_revision
        ):
            return {"ok": False, "reason": "core_applied_generation_unowned"}
        manifest_routing = manifest.get("routing_global_state")
        if not isinstance(manifest_routing, dict):
            return {"ok": False, "reason": "core_applied_routing_unavailable"}
        routing_keys = (
            "desired_mode", "applied_mode", "selective_default", "server_mode",
            "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until",
            "active_auto_server_id",
        )
        if any(
            str(manifest_routing.get(key) or "") != str(routing.get(key) or "")
            for key in routing_keys
        ):
            return {"ok": False, "reason": "core_applied_routing_mismatch"}
        mode = str(routing.get("desired_mode") or "").strip().lower()
        if mode not in {"direct", "selective", "vpn"} or routing.get("applied_mode") != mode:
            return {"ok": False, "reason": "core_routing_mode_unverified"}

        payload = dataplane_status._read_live_dataplane_payload()
        if not dataplane_status._live_owned_table_ready(payload):
            return {"ok": False, "reason": "core_live_dataplane_unready"}
        # Refresh the existing active-mode probe explicitly; runtime enforcement
        # consumes this freshly populated cache entry along with the payload.
        get_live_probe_cache(
            "dataplane_live.global_mode", ttl_seconds=2.0,
            loader=dataplane_live._probe_live_global_mode_uncached, force_refresh=True,
        )
        enforcement = dataplane_status.build_runtime_enforcement_state(live_payload=payload)
        expected_levels = {
            "direct": "global_direct_enforced",
            "selective": "global_selective_enforced",
            "vpn": "global_vpn_enforced",
        }
        if (
            not isinstance(enforcement, dict)
            or enforcement.get("enforcement_level") != expected_levels[mode]
            or enforcement.get("traffic_enforcement_guaranteed") is not True
            or enforcement.get("active_mode_matches_intent") is not True
            or str(enforcement.get("live_global_mode") or "").strip().lower() != mode
            or enforcement.get("bypass_active") is True
        ):
            return {"ok": False, "reason": "core_native_enforcement_unproven"}
        manifest_hash = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"),
        ).hexdigest()
        runtime_context_after = read_core_runtime_context()
        if runtime_context_after is None or runtime_context_after != runtime_context_before:
            return {"ok": False, "reason": "core_runtime_context_changed"}
        return {
            "ok": True,
            "apply_id": manifest_apply_id,
            "manifest_hash": manifest_hash,
            "enforcement_level": enforcement["enforcement_level"],
            "traffic_enforcement_guaranteed": True,
            "active_mode_matches_intent": True,
            "live_global_mode": mode,
            "live_selective_default": enforcement.get("live_selective_default"),
            "runtime_context": runtime_context_after,
        }
    except Exception:
        return {"ok": False, "reason": "core_native_readback_unavailable"}

def legacy_runtime_fence_facts(admission: Any) -> dict[str, bool]:
    """Return closed-vocabulary runtime fence facts from one fresh readback pass."""
    facts = {
        "xray_incarnation_match": False,
        "xray_runtime_config_match": False,
        "xray_active_config_match": False,
        "mihomo_incarnation_match": False,
        "mihomo_active_config_match": False,
        "mihomo_input_fingerprint_match": False,
        "mihomo_routing_snapshot_match": False,
        "core_runtime_fingerprint_match": False,
        "core_state_available": False,
        "core_native_proof_ok": False,
        "core_manifest_match": False,
        "core_apply_id_match": False,
        "core_enforcement_match": False,
        "core_live_mode_match": False,
    }
    try:
        adapter = _xray_adapter()
        from fwrouter_api.services import mihomo_config
        from fwrouter_api.services.mihomo_config_yaml import hash_mihomo_yaml_file
        from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint
        from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
        with db_session() as connection:
            core_fingerprint = _legacy_admission_core_runtime_fingerprint(connection)
            core_unavailable = _legacy_admission_core_unavailable_reason(connection)
        xray_sha = hashlib.sha256(Path(adapter.config_path).read_bytes()).hexdigest()
        mihomo_path = Path(mihomo_config._resolved_base_config_path())
        mihomo_sha = hash_mihomo_yaml_file(mihomo_path)
        selection = _capture_generation_auto_selection()
        expected_selection = getattr(admission, "proof_only_selection_snapshot", None)
        routing_snapshot = getattr(admission, "proof_only_routing_snapshot", None)
        # Keep the historical no-argument path for ordinary callers/adapters;
        # only an explicitly captured fixed projection is passed positionally.
        fingerprint = current_mihomo_input_fingerprint(routing_snapshot) if isinstance(routing_snapshot, dict) else current_mihomo_input_fingerprint()
        core_native = _collect_legacy_admission_core_native_proof()
        xray_incarnation_reader = getattr(adapter, "get_runtime_incarnation", None)
        xray_config_reader = getattr(adapter, "get_runtime_config_sha256", None)
        facts.update({
            "xray_incarnation_match": bool(
                callable(xray_incarnation_reader)
                and xray_incarnation_reader() == admission.proof_only_xray_incarnation
            ),
            "xray_runtime_config_match": bool(
                callable(xray_config_reader)
                and xray_config_reader() == admission.proof_only_xray_config_sha256
            ),
            "xray_active_config_match": (
                admission.proof_only_xray_config_sha256 == xray_sha
            ),
            "mihomo_incarnation_match": (
                get_mihomo_runtime_incarnation() == admission.proof_only_mihomo_incarnation
            ),
            "mihomo_active_config_match": (
                mihomo_sha == admission.proof_only_mihomo_config_sha256
            ),
            "mihomo_input_fingerprint_match": (
                fingerprint.get("hash") == admission.proof_only_mihomo_fingerprint_hash
            ),
            "mihomo_routing_snapshot_match": (
                isinstance(expected_selection, dict)
                and selection == expected_selection
                and isinstance(routing_snapshot, dict)
                and selection.get("routing") == routing_snapshot
            ),
            "core_runtime_fingerprint_match": (
                core_fingerprint == admission.proof_only_core_runtime_fingerprint
            ),
            "core_state_available": core_unavailable is None,
            "core_native_proof_ok": core_native.get("ok") is True,
            "core_manifest_match": (
                core_native.get("manifest_hash") == admission.proof_only_core_manifest_hash
            ),
            "core_apply_id_match": (
                core_native.get("apply_id") == admission.proof_only_core_apply_id
            ),
            "core_enforcement_match": (
                core_native.get("enforcement_level") == admission.proof_only_core_enforcement_level
            ),
            "core_live_mode_match": (
                core_native.get("live_global_mode") == admission.proof_only_core_live_mode
            ),
        })
    except Exception:
        return facts
    return facts

def legacy_runtime_fences_match(admission: Any) -> bool:
    """Check live native incarnation/config fences immediately before promotion."""
    required = {
        "xray_incarnation_match", "xray_runtime_config_match", "xray_active_config_match",
        "mihomo_incarnation_match", "mihomo_active_config_match", "mihomo_input_fingerprint_match",
        "mihomo_routing_snapshot_match",
        "core_runtime_fingerprint_match", "core_state_available", "core_native_proof_ok",
        "core_manifest_match", "core_apply_id_match", "core_enforcement_match", "core_live_mode_match",
    }
    facts = _legacy_admission_runtime_fence_facts(admission)
    return set(facts) == required and all(facts.get(key) is True for key in required)

def fixed_routing_projection(routing: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(routing, dict):
        return None
    return {key: routing.get(key) for key in _FIXED_XRAY_ROUTING_FIELDS}

def fixed_routing_snapshot_matches(
    snapshot: dict[str, Any] | None,
    workflow_revision: int | None,
    *, expected_selection_revision: int | None = None,
    origin_selection_revision: int | None = None,
) -> bool:
    """Rebind fixed candidate projection to the live route and owned workflow."""
    if not isinstance(snapshot, dict) or type(workflow_revision) is not int:
        return False
    try:
        from fwrouter_api.services.server_state import get_routing_global_state
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
        from fwrouter_api.services.core_intent import read_core_intent_revision

        routing = get_routing_global_state(expire_ttl=False)
        if not isinstance(routing, dict):
            return False
        if any(routing.get(key) != snapshot.get(key) for key in _FIXED_XRAY_ROUTING_FIELDS):
            return False
        if (routing.get("server_mode") != "fixed"
                or not routing.get("desired_fixed_server_id")
                or routing.get("applied_fixed_server_id") != routing.get("desired_fixed_server_id")
                or routing.get("apply_state") != "clean"):
            return False
        with db_session() as connection:
            pending = _load_row(connection)
            current_selection_revision = read_selection_revision(connection)
            core_revision = read_core_intent_revision(connection)
            core_row = connection.execute(
                "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
            ).fetchone()
            fixed_ttl_valid = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
        workflow = pending.get("workflow") if isinstance(pending, dict) else None
        if (type(expected_selection_revision) is not int
                or current_selection_revision != expected_selection_revision
                or type(origin_selection_revision) is not int
                or fixed_ttl_valid is None or not bool(fixed_ttl_valid[0])):
            return False
        if (not isinstance(workflow, dict)
                or workflow.get("selection_revision") != origin_selection_revision):
            return False
        if (core_row is None
                or tuple(str(core_row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
                    != tuple(workflow.get("core_snapshot") or ())
                or core_revision != workflow.get("core_revision")):
            return False
        core_proof = _collect_legacy_admission_core_native_proof()
        return bool(
            isinstance(pending, dict) and pending.get("pending") is True
            and pending.get("revision") == workflow_revision
            and isinstance(workflow, dict) and workflow.get("kind") == "global_fixed_server"
            and workflow.get("phase") == "core_verified"
            and workflow.get("server_id") == snapshot.get("desired_fixed_server_id")
            and workflow.get("fixed_until") == snapshot.get("fixed_server_until")
            and core_proof.get("ok") is True
            and core_proof.get("apply_id") == workflow.get("core_apply_id")
            and core_proof.get("manifest_hash") == workflow.get("core_manifest_hash")
            and core_proof.get("enforcement_level") == workflow.get("core_enforcement_level")
            and core_proof.get("live_global_mode") == workflow.get("core_live_mode")
        )
    except Exception:
        return False

def capture_ordinary_fixed_context() -> tuple[dict[str, Any] | None, str | None]:
    """Capture fixed intent for an ordinary Xray generation without claiming a workflow."""
    try:
        selection = _capture_generation_auto_selection()
        routing = selection.get("routing") if isinstance(selection, dict) else None
        if not isinstance(routing, dict) or routing.get("server_mode") != "fixed":
            return None, None
        target = str(routing.get("desired_fixed_server_id") or "").strip()
        if not target:
            # Preserve the existing targetless direct compatibility behavior.
            return None, None
        if (
            routing.get("applied_fixed_server_id") != target
            or routing.get("apply_state") != "clean"
            or routing.get("desired_mode") != routing.get("applied_mode")
        ):
            return None, "fixed_routing_state_unverified"
        with db_session() as connection:
            ttl_valid = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            core_unavailable = _legacy_admission_core_unavailable_reason(connection)
        if ttl_valid is None or not bool(ttl_valid[0]):
            return None, "fixed_server_ttl_unverified"
        if core_unavailable is not None:
            return None, core_unavailable
        core_runtime_fingerprint = _legacy_admission_core_runtime_fingerprint()
        core_proof = _collect_legacy_admission_core_native_proof()
        if core_proof.get("ok") is not True:
            return None, str(core_proof.get("reason") or "core_native_enforcement_unproven")
        selection_after_proof = _capture_generation_auto_selection()
        with db_session() as connection:
            ttl_after_proof = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            core_after_proof = _legacy_admission_core_unavailable_reason(connection)
            core_runtime_after_proof = _legacy_admission_core_runtime_fingerprint(connection)
        if (
            selection_after_proof != selection
            or ttl_after_proof is None or not bool(ttl_after_proof[0])
            or core_after_proof is not None
            or core_runtime_after_proof != core_runtime_fingerprint
        ):
            return None, "fixed_generation_context_changed_during_proof"
        return {
            "routing_snapshot": _fixed_xray_routing_projection(routing),
            "selection_snapshot": selection,
            "core_runtime_fingerprint": core_runtime_fingerprint,
            "core_proof": {
                key: core_proof.get(key) for key in (
                    "manifest_hash", "apply_id", "enforcement_level", "live_global_mode",
                )
            },
        }, None
    except Exception:
        return None, "fixed_generation_context_unavailable"

def ordinary_fixed_context_matches(
    context: dict[str, Any] | None, *, expected_selection_revision: int | None = None,
) -> bool:
    """Reprove durable fixed routing and its existing Core owner before/after each effect."""
    if not isinstance(context, dict):
        return False
    try:
        snapshot = context.get("routing_snapshot")
        selection_before = context.get("selection_snapshot")
        core_expected = context.get("core_proof")
        if not isinstance(snapshot, dict) or not isinstance(selection_before, dict) or not isinstance(core_expected, dict):
            return False
        current_selection = _capture_generation_auto_selection()
        revision = expected_selection_revision
        if revision is None:
            revision = selection_before.get("selection_revision")
        if (
            type(revision) is not int
            or current_selection.get("selection_revision") != revision
            or current_selection.get("selection_decision_id") != selection_before.get("selection_decision_id")
            or _fixed_xray_routing_projection(current_selection.get("routing")) != snapshot
        ):
            return False
        source_fingerprint = context.get("source_fingerprint")
        if (not isinstance(source_fingerprint, str) or len(source_fingerprint) != 64
                or _generation_source_fingerprint() != source_fingerprint):
            return False
        with db_session() as connection:
            if (
                _legacy_admission_core_unavailable_reason(connection) is not None
                or _legacy_admission_core_runtime_fingerprint(connection)
                != context.get("core_runtime_fingerprint")
            ):
                return False
            ttl_valid = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            if ttl_valid is None or not bool(ttl_valid[0]):
                return False
        proof = _collect_legacy_admission_core_native_proof()
        if proof.get("ok") is not True or not all(
            proof.get(key) == core_expected.get(key)
            for key in ("manifest_hash", "apply_id", "enforcement_level", "live_global_mode")
        ):
            return False
        selection_after_proof = _capture_generation_auto_selection()
        with db_session() as connection:
            ttl_after_proof = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            core_after_proof = _legacy_admission_core_unavailable_reason(connection)
            core_runtime_after_proof = _legacy_admission_core_runtime_fingerprint(connection)
        return bool(
            selection_after_proof.get("selection_revision") == revision
            and selection_after_proof.get("selection_decision_id") == selection_before.get("selection_decision_id")
            and _fixed_xray_routing_projection(selection_after_proof.get("routing")) == snapshot
            and ttl_after_proof is not None and bool(ttl_after_proof[0])
            and core_after_proof is None
            and core_runtime_after_proof == context.get("core_runtime_fingerprint")
            and _generation_source_fingerprint() == source_fingerprint
        )
    except Exception:
        return False

def fixed_generation_context_matches(
    routing_snapshot: dict[str, Any] | None,
    fixed_workflow_revision: int | None,
    *,
    expected_selection_revision: int | None = None,
    fixed_origin_selection_revision: int | None = None,
    ordinary_fixed_context: dict[str, Any] | None = None,
) -> bool:
    if fixed_workflow_revision is not None:
        return _fixed_xray_routing_snapshot_matches(
            routing_snapshot, fixed_workflow_revision,
            expected_selection_revision=expected_selection_revision,
            origin_selection_revision=fixed_origin_selection_revision,
        )
    if ordinary_fixed_context is not None:
        return (
            routing_snapshot == ordinary_fixed_context.get("routing_snapshot")
            and _ordinary_fixed_xray_context_matches(
                ordinary_fixed_context, expected_selection_revision=expected_selection_revision,
            )
        )
    return True

def capture_generation_selection() -> dict[str, Any]:
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    with db_session() as connection:
        routing = connection.execute(
            "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
            "active_auto_server_id, desired_mode, applied_mode, selective_default, apply_state, updated_at "
            "FROM routing_global_state WHERE id = 1"
        ).fetchone()
        provenance = connection.execute(
            "SELECT value_json, updated_at FROM settings WHERE key = 'routing.auto_selection_provenance'"
        ).fetchone()
        fence = read_selection_fence(connection)
    return {
        "routing": dict(routing) if routing else None,
        "provenance": dict(provenance) if provenance else None,
        "selection_revision": fence["revision"],
        "selection_decision_id": fence["decision_id"],
    }

def verify_generation_selection(selection: dict[str, Any] | None) -> dict[str, Any]:
    routing = (selection or {}).get("routing") if isinstance(selection, dict) else None
    if not isinstance(routing, dict):
        return {"ok": False, "error_code": "SELECTION_BASELINE_UNAVAILABLE"}
    mode = str(routing.get("server_mode") or "auto").lower()
    logical_id = str(
        routing.get("desired_fixed_server_id") or routing.get("applied_fixed_server_id") or ""
        if mode == "fixed"
        else routing.get("active_auto_server_id") or ""
    ).strip()
    if not logical_id:
        return {"ok": False, "error_code": "SELECTION_BASELINE_UNSELECTED"}
    from fwrouter_api.services.logical_topology import get_logical_runtime_name
    from fwrouter_api.services.selector import get_vpn_auto_state
    from fwrouter_api.services.servers import get_routing_global_state
    state = get_vpn_auto_state(read_only=True)
    current_routing = get_routing_global_state(expire_ttl=False) or {}
    selectors = state.get("selector_runtime") if isinstance(state.get("selector_runtime"), dict) else {}
    selector_name = "vpn_global_now" if mode == "fixed" else "vpn_auto_now"
    expected = get_logical_runtime_name(logical_id)
    observed = str(selectors.get(selector_name) or "").strip()
    current_logical = (
        str(current_routing.get("desired_fixed_server_id") or current_routing.get("applied_fixed_server_id") or "").strip()
        if mode == "fixed"
        else str(state.get("active_auto_server_id") or "").strip()
    )
    ok = bool(expected and observed and current_logical == logical_id and observed == expected)
    return {
        "ok": ok,
        "mode": mode,
        "logical_server_id": logical_id,
        "expected_effective_target": expected,
        "effective_target": observed or None,
        "error_code": None if ok else "SELECTION_RESTORE_READBACK_UNCONFIRMED",
    }

def ordinary_fixed_checkpoint_context_matches(
    checkpoint: dict[str, Any], *, expected_source: str, expected_revision: int,
) -> bool:
    """Revalidate the checkpoint's original ordinary-fixed Core/routing owner."""
    route_snapshot = checkpoint.get("fixed_routing_snapshot")
    if route_snapshot is None:
        return True
    if not isinstance(route_snapshot, dict):
        return False
    fixed_workflow_revision = checkpoint.get("fixed_workflow_revision")
    if type(fixed_workflow_revision) is int:
        # Fixed-workflow publication is fenced by its existing callback and
        # workflow receipt; this helper adds proof only for ordinary fixed
        # startup checkpoints.
        return True
    selection_snapshot = checkpoint.get("auto_selection_after")
    if not isinstance(selection_snapshot, dict):
        selection_snapshot = checkpoint.get("auto_selection_before")
    core_proof = checkpoint.get("ordinary_fixed_core_proof")
    core_runtime_fingerprint = checkpoint.get("ordinary_fixed_core_runtime_fingerprint")
    if (
        not isinstance(selection_snapshot, dict)
        or selection_snapshot.get("selection_revision") != expected_revision
        or checkpoint.get("selection_revision") != expected_revision
        or not isinstance(core_proof, dict)
        or not isinstance(core_runtime_fingerprint, str)
        or len(core_runtime_fingerprint) != 64
        or not isinstance(expected_source, str)
        or len(expected_source) != 64
    ):
        return False
    context = {
        "routing_snapshot": route_snapshot,
        "selection_snapshot": selection_snapshot,
        "core_proof": core_proof,
        "core_runtime_fingerprint": core_runtime_fingerprint,
        "source_fingerprint": expected_source,
    }
    return _fixed_xray_generation_context_matches(
        route_snapshot, None,
        expected_selection_revision=expected_revision,
        ordinary_fixed_context=context,
    )

_generation_source_fingerprint = generation_source_fingerprint
_legacy_admission_core_runtime_snapshot = legacy_core_runtime_snapshot
_legacy_admission_core_unavailable_reason = legacy_core_unavailable_reason
_legacy_admission_core_runtime_fingerprint = legacy_core_runtime_fingerprint
_collect_legacy_admission_core_native_proof = collect_core_native_proof
_legacy_admission_runtime_fence_facts = legacy_runtime_fence_facts
_fixed_xray_routing_projection = fixed_routing_projection
_fixed_xray_routing_snapshot_matches = fixed_routing_snapshot_matches
_ordinary_fixed_xray_context_matches = ordinary_fixed_context_matches
_fixed_xray_generation_context_matches = fixed_generation_context_matches
_capture_generation_auto_selection = capture_generation_selection
