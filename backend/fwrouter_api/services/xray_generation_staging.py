"""Private candidate preparation and validation for Xray generations.

This module may create and validate private temporary/staged candidates and
write bounded technical diagnostics. It does not activate candidates, advance
operation phases, write checkpoints, publish receipts, or own leases/CAS.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import write_all_fd as _write_all_fd
from fwrouter_api.services.custom_servers import VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.subscription_profiles import (
    list_desired_subscription_xray_clients,
    list_subscription_profile_tokens,
)
from fwrouter_api.services.xray_admission_config_proof import (
    _legacy_mihomo_candidate_matches,
    _legacy_xray_candidate_matches_active,
)
from fwrouter_api.services.xray_bindings import _applied_handoff_assignments
from fwrouter_api.services.xray_generation_observation import (
    capture_generation_selection as _capture_generation_auto_selection,
    collect_core_native_proof as _collect_legacy_admission_core_native_proof,
    fixed_generation_context_matches as _fixed_xray_generation_context_matches,
    fixed_routing_projection as _fixed_xray_routing_projection,
    fixed_routing_snapshot_matches as _fixed_xray_routing_snapshot_matches,
    legacy_admission_source_fingerprint as _legacy_admission_source_fingerprint,
    legacy_core_runtime_fingerprint as _legacy_admission_core_runtime_fingerprint,
    legacy_core_unavailable_reason as _legacy_admission_core_unavailable_reason,
)
from fwrouter_api.services.xray_generation_views import (
    bindings_artifact_parity as _current_bindings_artifact_parity,
    capture_current_projection as _current_xray_projection_snapshot,
    current_projection_fingerprint as _current_projection_fingerprint,
    verify_recovery_selection as _verify_generation_recovery_selection_readback,
)
from fwrouter_api.services.xray_common import _strip_raw_payload
from fwrouter_api.services.xray_profile_planning import (
    prepare_profile_bindings as _prospective_profile_bindings,
    read_vpn_auto_candidates as _vpn_auto_servers_for_xray_subscription,
)
from fwrouter_api.services.xray_runtime_state import _module_state

__all__ = [
    "collect_legacy_inventory_proof",
    "log_input_context_change",
    "prepare_native_candidates",
    "summarize_input_context",
    "validate_mihomo_preimage",
]

def prepare_native_candidates(
    *,
    adapter: Any,
    desired_clients: list[dict[str, Any]],
    managed_email_prefixes: list[str],
    bindings: list[dict[str, Any]],
    client_modes: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    routing_snapshot: dict[str, Any] | None = None,
    fixed_workflow_revision: int | None = None,
    fixed_origin_selection_revision: int | None = None,
    ordinary_fixed_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Native-validate prospective Xray plus transition/final Mihomo configs."""
    config_path = getattr(adapter, "config_path", None)
    if config_path is None or not callable(getattr(adapter, "stage_subscription_generation", None)):
        return {"ok": False, "stage": "stage_api", "error_code": "XRAY_GENERATION_STAGE_UNAVAILABLE"}
    routing_snapshot = _fixed_xray_routing_projection(routing_snapshot)
    if (fixed_workflow_revision is not None
            and not _fixed_xray_routing_snapshot_matches(
                routing_snapshot, fixed_workflow_revision,
                expected_selection_revision=fixed_origin_selection_revision,
                origin_selection_revision=fixed_origin_selection_revision,
            )):
        return {"ok": False, "stage": "fixed_routing_snapshot", "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE"}
    ordinary_origin_revision = (
        ordinary_fixed_context.get("selection_snapshot", {}).get("selection_revision")
        if isinstance(ordinary_fixed_context, dict) else None
    )
    if (ordinary_fixed_context is not None and not _fixed_xray_generation_context_matches(
            routing_snapshot, None, expected_selection_revision=ordinary_origin_revision,
            ordinary_fixed_context=ordinary_fixed_context)):
        return {"ok": False, "stage": "fixed_routing_snapshot", "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE"}
    stage_dir = Path(config_path).parent / ".generation"
    stage_dir.mkdir(parents=True, exist_ok=True)
    stage_dir.chmod(0o700)
    xray_candidate = stage_dir / "xray.candidate.json"
    old_assignments = _applied_handoff_assignments()
    transition_by_target = {
        str(item.get("selected_server_id") or ""): dict(item)
        for item in old_assignments
        if str(item.get("selected_server_id") or "")
    }
    transition_by_target.update({
        str(item.get("selected_server_id") or ""): dict(item)
        for item in assignments
        if str(item.get("selected_server_id") or "")
    })
    transition_assignments = list(transition_by_target.values())
    xray_result = adapter.stage_subscription_generation(
        desired_clients=desired_clients,
        managed_email_prefixes=managed_email_prefixes,
        bindings=bindings,
        client_modes=client_modes,
        handoff_assignments=assignments,
        candidate_path=xray_candidate,
    )
    if not xray_result.ok:
        return {"ok": False, "stage": "xray_candidate", "result": _strip_raw_payload(xray_result.details), "error_code": xray_result.error_code}

    from fwrouter_api.services.mihomo_config import (
        validate_mihomo_candidate_config as validate_local_mihomo,
        write_mihomo_candidate_config,
    )
    from fwrouter_api.services.subscription_pipeline import (
        resolve_mihomo_validator_image_id,
        validate_mihomo_candidate_config as validate_native_mihomo,
    )

    native: dict[str, Any] = {"xray": dict(xray_result.details)}
    staged_mihomo: dict[str, str] = {}
    validated_native_candidates: dict[tuple[str, str], dict[str, Any]] = {}
    native_image_id = resolve_mihomo_validator_image_id()
    native_validator_identity = (
        f"docker:{native_image_id}:network-none:read-only" if native_image_id else None
    )
    def candidate_input_context():
        from fwrouter_api.services.mihomo_config_inbounds import _candidate_base_config_paths
        from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint, _file_hash
        try:
            paths = _candidate_base_config_paths()
            if len(paths) > 8:
                return None  # Avoid hashing unbounded historical fallback files.
            fingerprint = (
                current_mihomo_input_fingerprint(routing_snapshot)
                if routing_snapshot is not None else current_mihomo_input_fingerprint()
            )
            if (not isinstance(fingerprint, dict)
                    or not isinstance(fingerprint.get("hash"), str)
                    or len(fingerprint["hash"]) != 64):
                return None
            return (fingerprint, tuple((str(path), _file_hash(path)) for path in paths), write_mihomo_candidate_config)
        except (OSError, ValueError, sqlite3.Error):
            return None  # Unknown context keeps the original full render/validation.

    transition_context = None
    transition_written = None
    for name, handoffs in (("transition", transition_assignments), ("final", assignments)):
        candidate_path = stage_dir / f"mihomo-{name}.candidate.yaml"
        context_before = candidate_input_context()
        write_reused = bool(name == "final" and transition_written is not None
                            and transition_assignments == assignments
                            and transition_context is not None and context_before == transition_context)
        if write_reused:
            from fwrouter_api.services.artifacts import atomic_copy_file
            transition_path = Path(staged_mihomo["transition"])
            expected_transition_hash = native["transition"]["candidate_sha256"]
            if hashlib.sha256(transition_path.read_bytes()).hexdigest() != expected_transition_hash:
                return {"ok": False, "stage": "mihomo_final_candidate", "error_code": "MIHOMO_STAGE_CANDIDATE_CHANGED_AFTER_VALIDATION"}
            atomic_copy_file(transition_path, candidate_path)
            written = transition_written
        else:
            written = write_mihomo_candidate_config(
                routing=routing_snapshot,
                candidate_path=candidate_path,
                xray_handoff_assignments=handoffs,
                include_internal_config=True,
            )
        if name == "transition":
            context_after = candidate_input_context()
            transition_context = context_before if context_before is not None and context_before == context_after else None
            if context_before is not None and context_after != context_before:
                _log_mihomo_input_context_change(
                    stage="transition_render",
                    before=context_before,
                    after=context_after,
                )
            transition_written = written
        candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        if write_reused and candidate_hash != expected_transition_hash:
            return {"ok": False, "stage": "mihomo_final_candidate", "error_code": "MIHOMO_STAGE_CANDIDATE_COPY_MISMATCH"}
        candidate_config = written.get("_candidate_config") if isinstance(written.get("_candidate_config"), dict) else {}
        if routing_snapshot is None:
            local_validation = validate_local_mihomo(
                candidate_path=candidate_path, candidate_config=candidate_config,
            )
        else:
            local_validation = validate_local_mihomo(
                routing=routing_snapshot,
                candidate_path=candidate_path,
                candidate_config=candidate_config,
            )
        native_cache_key = (candidate_hash, native_validator_identity or "")
        cached_native_validation = (
            validated_native_candidates.get(native_cache_key)
            if native_validator_identity is not None
            else None
        )
        native_validation = (
            dict(cached_native_validation)
            if cached_native_validation is not None
            else (
                validate_native_mihomo(str(candidate_path), image_reference=native_image_id)
                if native_image_id is not None
                else validate_native_mihomo(str(candidate_path))
            )
        )
        validation = {
            "ok": bool(local_validation.get("ok")) and bool(native_validation.get("ok")),
            "local": _strip_raw_payload(local_validation),
            "native": _strip_raw_payload(native_validation),
            "native_validation_reused": cached_native_validation is not None,
            "candidate_write_reused": write_reused,
        }
        if not validation["ok"]:
            return {
                "ok": False,
                "stage": f"mihomo_{name}_candidate",
                "error_code": "MIHOMO_CANDIDATE_INVALID",
                "validation": _strip_raw_payload(validation),
            }
        context_after_validation = candidate_input_context()
        transition_context_changed = name == "final" and transition_context != context_before
        if context_before is None or context_after_validation != context_before or transition_context_changed:
            _log_mihomo_input_context_change(
                stage=f"{name}_native_validation",
                before=context_before,
                after=(transition_context if transition_context_changed else context_after_validation),
            )
            return {"ok": False, "stage": f"mihomo_{name}_candidate",
                    "error_code": "MIHOMO_STAGE_INPUTS_CHANGED_AFTER_VALIDATION"}
        if not _fixed_xray_generation_context_matches(
                routing_snapshot, fixed_workflow_revision,
                expected_selection_revision=(
                    fixed_origin_selection_revision if fixed_workflow_revision is not None
                    else ordinary_origin_revision
                ),
                fixed_origin_selection_revision=fixed_origin_selection_revision,
                ordinary_fixed_context=ordinary_fixed_context):
            return {"ok": False, "stage": f"mihomo_{name}_candidate",
                    "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE"}
        if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != candidate_hash:
            return {"ok": False, "stage": f"mihomo_{name}_candidate", "error_code": "MIHOMO_STAGE_CANDIDATE_CHANGED_DURING_VALIDATION"}
        if cached_native_validation is None and native_validator_identity is not None:
            validated_native_candidates[native_cache_key] = dict(native_validation)
        native[name] = {
            "candidate_path": str(candidate_path),
            "candidate_sha256": candidate_hash,
            "validation": _strip_raw_payload(validation),
            "write": _strip_raw_payload(written),
        }
        staged_mihomo[name] = str(candidate_path)
    return {
        "ok": True,
        "stage_dir": str(stage_dir),
        "xray_candidate_path": str(xray_candidate),
        "xray_candidate_sha256": str(xray_result.details.get("candidate_sha256") or ""),
        "mihomo_candidates": staged_mihomo,
        "native_validation": native,
        # Staged files are generated away from config.next.yaml, so they do
        # not receive the default writer's provenance sidecar. Carry the
        # exact input revision through the existing stage validation and bind
        # it when the validated bytes are copied to the default candidate.
        "mihomo_input_fingerprint": (
            {"hash": transition_context[0]["hash"]}
            if transition_context is not None else None
        ),
        # The raw fixed projection is private handoff state consumed only by
        # the operation checkpoint writer. It is not included in public
        # diagnostics or publication results.
        "fixed_routing_snapshot": (
            _fixed_xray_routing_projection(routing_snapshot)
            if isinstance(routing_snapshot, dict) and routing_snapshot.get("server_mode") == "fixed"
            else None
        ),
    }

def collect_legacy_inventory_proof(
    adapter: Any,
    *,
    expected_digest: str,
    source_fingerprint: str,
    checkpoint_path: Path,
) -> dict[str, Any]:
    """Build private candidates and prove they equal the currently loaded native state.

    This deliberately does not call the regular staging owner: that helper
    writes shared candidate paths. The temporary directory is private and is
    removed on return; this function never applies, reloads, or publishes.
    """
    from fwrouter_api.services import mihomo_config, subscription_profiles
    from fwrouter_api.services.mihomo_reconcile import _native_udp_policy_proof
    from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.subscription import _subscription_inventory_digest
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    from fwrouter_api.services.xray_materialize import (
        _verify_active_config_bindings, _verify_active_config_client_modes,
    )
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path

    if not xray_writer_guard_is_held():
        return {"ok": False, "reason": "writer_guard_required"}
    module = _module_state("xray") or {}
    # The managed Mihomo dataplane adapter is owned by the existing `vpn`
    # module lifecycle row; there is no standalone `mihomo` module row.
    vpn_module = _module_state("vpn") or {}
    if not (
        module.get("desired_state") == "enabled" and module.get("lifecycle_mode") == "managed"
        and vpn_module.get("desired_state") == "enabled" and vpn_module.get("lifecycle_mode") == "managed"
    ):
        return {"ok": False, "reason": "managed_native_runtimes_required"}
    try:
        with db_session() as connection:
            digest, count = _subscription_inventory_digest(connection)
            source_now = _legacy_admission_source_fingerprint(connection)
            selection = read_selection_fence(connection)
            core_unavailable = _legacy_admission_core_unavailable_reason(connection)
            core_runtime_fingerprint = _legacy_admission_core_runtime_fingerprint(connection)
        if digest != expected_digest or count <= 0 or source_now != source_fingerprint:
            return {"ok": False, "reason": "durable_source_changed"}
        if core_unavailable is not None:
            return {"ok": False, "reason": core_unavailable}
        core_native_proof = _collect_legacy_admission_core_native_proof()
        if core_native_proof.get("ok") is not True:
            return {"ok": False, "reason": core_native_proof.get("reason", "core_native_enforcement_unproven")}
        if not callable(getattr(adapter, "stage_subscription_generation", None)):
            return {"ok": False, "reason": "xray_candidate_validation_unavailable"}
        xray_path = Path(adapter.config_path)
        active_xray_sha = hashlib.sha256(xray_path.read_bytes()).hexdigest()
        xray_incarnation = adapter.get_runtime_incarnation()
        mounted_xray_sha = adapter.get_runtime_config_sha256()
        if not xray_incarnation or active_xray_sha != mounted_xray_sha:
            return {"ok": False, "reason": "xray_active_runtime_mismatch"}
        mihomo_path = Path(mihomo_config._resolved_base_config_path())
        from fwrouter_api.services.mihomo_config_yaml import hash_mihomo_yaml_file
        try:
            active_mihomo_sha = hash_mihomo_yaml_file(mihomo_path)
        except ValueError:
            return {"ok": False, "reason": "mihomo_active_config_unavailable"}
        mihomo_incarnation = get_mihomo_runtime_incarnation()
        if not mihomo_incarnation:
            return {"ok": False, "reason": "mihomo_runtime_incarnation_unavailable"}
        expected_selection = _capture_generation_auto_selection()
        routing_snapshot = expected_selection.get("routing")
        if not isinstance(routing_snapshot, dict):
            return {"ok": False, "reason": "selection_fence_unavailable"}
        if (
            expected_selection.get("selection_revision") != selection.get("revision")
            or expected_selection.get("selection_decision_id") != selection.get("decision_id")
        ):
            return {"ok": False, "reason": "selection_fence_changed"}
        input_fingerprint = current_mihomo_input_fingerprint(routing_snapshot)
        fingerprint_hash = str(input_fingerprint.get("hash") or "")
        if len(fingerprint_hash) != 64:
            return {"ok": False, "reason": "mihomo_input_fingerprint_unavailable"}

        # Refuse deterministic fallback credentials. Proof-only admission may
        # verify identities already present in durable mapping; it never
        # creates, registers, repairs, or rotates them.
        with db_session() as connection:
            from fwrouter_api.services.subscription_node_identity import credentials_for_scope
            profile_tokens = list_subscription_profile_tokens()
            profile_servers = subscription_profiles._subscription_servers()
            for token in profile_tokens:
                rows = credentials_for_scope(connection, "subscription:" + token)
                for server in profile_servers:
                    server_id = str(server.get("server_id") or "")
                    if server_id == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
                        continue
                    credential = rows.get(server_id)
                    if credential is None:
                        return {"ok": False, "reason": "durable_profile_credential_missing"}
            retained_auto = credentials_for_scope(connection, "vpn-auto")
        auto_servers = _vpn_auto_servers_for_xray_subscription()
        desired_nodes = list_desired_subscription_xray_clients()
        for server in auto_servers:
            server_id = str(server.get("server_id") or "")
            credential = retained_auto.get(server_id)
            if credential is None:
                return {"ok": False, "reason": "durable_auto_credential_missing"}
            if credential.get("revoked"):
                continue
            desired_nodes.append({
                "server_id": server_id,
                "server_name": str(server.get("server_name") or server_id),
                "client_uuid": str(credential["client_uuid"]),
                "client_email": str(credential["client_email"]),
                "xray_alias": str(server.get("server_name") or server_id),
                "vpn_auto": True,
            })
        desired_by_email = {
            str(node.get("client_email") or ""): node
            for node in desired_nodes if str(node.get("client_email") or "").strip()
        }
        if len(desired_by_email) != len(desired_nodes):
            return {"ok": False, "reason": "durable_identity_projection_ambiguous"}
        desired_clients = [{
            "client_uuid": node["client_uuid"], "client_id": node["client_uuid"],
            "email": node["client_email"], "alias": node.get("xray_alias") or "",
        } for node in desired_nodes]
        managed_prefixes = ["sub-", "vpn-auto-"]
        bindings, modes, assignments = _prospective_profile_bindings(
            desired_nodes, token_prefix="sub-", managed_email_prefixes=managed_prefixes,
            preserve_existing_overrides=True,
        )
        selection_proof = _verify_generation_recovery_selection_readback(expected_selection)
        if not selection_proof.get("ok"):
            return {"ok": False, "reason": "core_selection_readback_unproven"}
        current_projection = _current_xray_projection_snapshot()
        if current_projection.get("fence", {}).get("revision") != selection.get("revision"):
            return {"ok": False, "reason": "selection_fence_changed"}
        artifact_ok, bindings_artifact_sha = _current_bindings_artifact_parity(bindings, modes)
        if not artifact_ok:
            return {"ok": False, "reason": "xray_bindings_artifact_mismatch"}
        with tempfile.TemporaryDirectory(prefix="fwrouter-legacy-admission-") as raw_dir:
            private_dir = Path(raw_dir)
            private_dir.chmod(0o700)
            xray_candidate = private_dir / "xray-candidate.json"
            staged = adapter.stage_subscription_generation(
                desired_clients=desired_clients, managed_email_prefixes=managed_prefixes,
                bindings=bindings, client_modes=modes, handoff_assignments=assignments,
                candidate_path=xray_candidate,
            )
            if not staged.ok:
                return {"ok": False, "reason": "xray_candidate_native_validation_failed"}
            if not _legacy_xray_candidate_matches_active(xray_path, xray_candidate):
                return {"ok": False, "reason": "xray_candidate_differs_from_active"}
            expected_ids = sorted((str(item[0]), str(item[1])) for item in staged.details.get("expected_client_identities", []))
            loaded_ids = sorted((str(pair[0]), str(pair[1])) for pair in adapter.list_loaded_client_identities())
            if not expected_ids or loaded_ids != expected_ids:
                return {"ok": False, "reason": "xray_handler_users_mismatch"}
            binding_readback = _verify_active_config_bindings(
                bindings, expected_client_identities=expected_ids,
            )
            mode_readback = _verify_active_config_client_modes(modes)
            if not binding_readback.get("ok") or not mode_readback.get("ok"):
                return {"ok": False, "reason": "xray_native_binding_or_mode_mismatch"}

            candidate_mihomo = mihomo_config.build_mihomo_config(
                routing=routing_snapshot,
                xray_handoff_assignments=assignments,
            )
            import yaml
            mihomo_candidate = private_dir / "mihomo-candidate.yaml"
            mihomo_candidate.write_text(
                yaml.safe_dump(candidate_mihomo, sort_keys=False, allow_unicode=True), encoding="utf-8",
            )
            mihomo_candidate.chmod(0o600)
            local_validation = mihomo_config.validate_mihomo_candidate_config(
                routing=routing_snapshot,
                candidate_config=candidate_mihomo, candidate_path=mihomo_candidate,
            )
            if not local_validation.get("ok"):
                return {"ok": False, "reason": "mihomo_candidate_validation_failed"}
            if not _legacy_mihomo_candidate_matches(mihomo_path, mihomo_candidate):
                return {"ok": False, "reason": "mihomo_candidate_differs_from_active"}
            native = _native_udp_policy_proof()
            if not (
                native.get("ok") is True
                and native.get("native_top_level_rules_match") is True
                and native.get("selector_membership_matches") is True
                and native.get("guarded_rules_match") is True
            ):
                return {"ok": False, "reason": "mihomo_native_policy_unproven"}
        # Detect source/runtime changes that occurred while expensive native
        # readbacks were in flight. The caller repeats CAS at marker promotion.
        core_native_recheck = _collect_legacy_admission_core_native_proof()
        if (
            _legacy_admission_source_fingerprint() != source_fingerprint
            or _legacy_admission_core_unavailable_reason() is not None
            or _legacy_admission_core_runtime_fingerprint() != core_runtime_fingerprint
            or _capture_generation_auto_selection() != expected_selection
            or adapter.get_runtime_incarnation() != xray_incarnation
            or adapter.get_runtime_config_sha256() != active_xray_sha
            or get_mihomo_runtime_incarnation() != mihomo_incarnation
            or hash_mihomo_yaml_file(mihomo_path) != active_mihomo_sha
            or _capture_generation_auto_selection() != expected_selection
            or current_mihomo_input_fingerprint(routing_snapshot).get("hash") != fingerprint_hash
            or hashlib.sha256(Path(_xray_bindings_path()).read_bytes()).hexdigest() != bindings_artifact_sha
            or _current_projection_fingerprint(_current_xray_projection_snapshot())
            != _current_projection_fingerprint(current_projection)
            or core_native_recheck.get("ok") is not True
            or core_native_recheck.get("manifest_hash") != core_native_proof.get("manifest_hash")
            or core_native_recheck.get("apply_id") != core_native_proof.get("apply_id")
            or core_native_recheck.get("enforcement_level") != core_native_proof.get("enforcement_level")
            or core_native_recheck.get("live_global_mode") != core_native_proof.get("live_global_mode")
        ):
            return {"ok": False, "reason": "native_proof_fence_changed"}
        return {
            "ok": True, "xray_config_match": True, "xray_loaded_users_match": True,
            "xray_bindings_match": True, "xray_modes_match": True,
            "core_selection_match": True, "mihomo_candidate_match": True,
            "mihomo_native_policy_match": True, "source_fingerprint": source_fingerprint,
            "selection_revision": selection.get("revision"),
            "selection_decision_id": selection.get("decision_id"),
            "selection_snapshot": expected_selection,
            "routing_snapshot": routing_snapshot,
            "xray_runtime_incarnation": xray_incarnation,
            "mihomo_runtime_incarnation": mihomo_incarnation,
            "xray_config_sha256": active_xray_sha,
            "mihomo_config_sha256": active_mihomo_sha,
            "mihomo_fingerprint_hash": fingerprint_hash,
            "core_runtime_fingerprint": core_runtime_fingerprint,
            "core_dataplane_proof": core_native_proof,
            "core_native_policy_match": True,
            "mihomo_proof": {
                "native_top_level_rules_match": True,
                "selector_membership_matches": True,
                "guarded_rules_match": True,
            },
        }
    except Exception as exc:
        name = type(exc).__name__
        safe_name = name if name.isascii() and name.isalnum() and name.endswith(("Error", "Exception")) else "proof_error"
        return {"ok": False, "reason": safe_name}

def validate_mihomo_preimage(
    encoded_config: Any,
    *,
    routing_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Prove a checkpoint's Mihomo preimage is safe before restoring it.

    Checkpoint identity parity says nothing about the routing policy in its
    saved Mihomo file. Validate the exact preimage, while it is still private,
    against the current FWRouter-owned routing contract and the real Mihomo
    parser. This deliberately refuses old/unknown policy instead of activating
    it and discovering the downgrade during post-apply readback.
    """
    if not isinstance(encoded_config, str):
        return {"ok": False, "reason": "mihomo_preimage_unavailable"}
    import yaml
    try:
        max_config_bytes = 64 * 1024 * 1024
        max_encoded_chars = ((max_config_bytes + 2) // 3) * 4
        if not encoded_config.isascii() or len(encoded_config) > max_encoded_chars:
            return {"ok": False, "reason": "mihomo_preimage_invalid"}
        config_bytes = base64.b64decode(encoded_config, validate=True)
        # Generated configs can contain the accepted ruleset up to the API's
        # existing 64 MiB policy-input ceiling (hosted evidence reached 20.7
        # MB). The 4 MiB base-user-config limit is not applicable here.
        if not config_bytes or len(config_bytes) > max_config_bytes:
            return {"ok": False, "reason": "mihomo_preimage_invalid"}
        config_text = config_bytes.decode("utf-8")
        candidate = yaml.load(config_text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except (ValueError, UnicodeDecodeError, yaml.YAMLError):
        return {"ok": False, "reason": "mihomo_preimage_invalid"}
    if not isinstance(candidate, dict):
        return {"ok": False, "reason": "mihomo_preimage_invalid"}

    # The generated routing rules include explicit DIRECT exceptions and the
    # mandatory VPN fail-closed rules. Exact comparison preserves those
    # established semantics while refusing a checkpoint from an older policy
    # generator. Do not compare proxies here: subscription topology may have
    # changed, and native parsing below independently checks all references.
    try:
        if not isinstance(routing_snapshot, dict):
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
        from fwrouter_api.services.mihomo_config_rules import _build_mihomo_config_with_source
        from fwrouter_api.services.mihomo_config_paths import (
            FULL_VPN_RULE_NAME, TRANSPARENT_TPROXY_RULE_NAME,
        )
        expected_rules, expected_policy = _build_mihomo_config_with_source(routing_snapshot)
        sub_rules = candidate.get("sub-rules")
        if not isinstance(sub_rules, dict):
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
        if candidate.get("rules") != expected_rules:
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
        if sub_rules.get(FULL_VPN_RULE_NAME) != expected_policy.get("full_vpn_rules"):
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
        if sub_rules.get(TRANSPARENT_TPROXY_RULE_NAME) != expected_policy.get("transparent_rules"):
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}

        groups = candidate.get("proxy-groups")
        if not isinstance(groups, list):
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
        group_names = [
            str(group.get("name") or "").strip()
            for group in groups if isinstance(group, dict) and str(group.get("name") or "").strip()
        ]
        by_name = {
            str(group.get("name") or "").strip(): group
            for group in groups if isinstance(group, dict) and str(group.get("name") or "").strip()
        }
        if len(group_names) != len(by_name):
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
        for required_name in ("vpn-auto", "vpn-global"):
            group = by_name.get(required_name)
            targets = group.get("proxies") if isinstance(group, dict) else None
            if not isinstance(targets, list) or not targets or targets[-1] != "REJECT" or "DIRECT" in targets:
                return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
        if "vpn-auto" not in by_name["vpn-global"].get("proxies", []):
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}

        mandatory_names = [
            name for name in by_name
            if name in {"vpn-auto", "vpn-global"} or name.startswith("fwrouter-subject-")
        ]
        group_edges = sum(
            len(group.get("proxies"))
            for group in by_name.values()
            if isinstance(group.get("proxies"), list)
        )
        # Bound traversal by the actual graph size, not inventory size. A
        # selector with many concrete proxy targets is valid and must not be
        # rejected just because it has few groups.
        policy_walk_budget = min(max(group_edges + len(by_name) + len(mandatory_names), 1), 100_000)
        policy_walk_count = 0
        completed_group_results: dict[str, bool] = {}

        def mandatory_target_is_fail_closed(name: str, visiting: set[str]) -> bool:
            nonlocal policy_walk_count
            policy_walk_count += 1
            if policy_walk_count > policy_walk_budget:
                return False
            if name == "DIRECT":
                return False
            if name == "REJECT":
                return True
            nested = by_name.get(name)
            if nested is None:
                # A concrete native proxy is an eligible egress target; native
                # validation below rejects unknown names and malformed proxies.
                return True
            if name in visiting:
                return False
            if name in completed_group_results:
                return completed_group_results[name]
            nested_targets = nested.get("proxies")
            if not isinstance(nested_targets, list) or not nested_targets:
                return False
            next_visiting = {*visiting, name}
            result = all(
                mandatory_target_is_fail_closed(str(target), next_visiting)
                for target in nested_targets
            )
            completed_group_results[name] = result
            return result

        for name, group in by_name.items():
            if name.startswith("fwrouter-subject-"):
                targets = group.get("proxies")
                if not isinstance(targets, list) or not targets or targets[-1] != "REJECT" or "DIRECT" in targets:
                    return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
        if any(
            not mandatory_target_is_fail_closed(name, set())
            for name in mandatory_names
        ):
            return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
    except Exception:
        return {"ok": False, "reason": "mihomo_preimage_policy_unproven"}

    # Keep the candidate on the existing shared config mount so the Docker
    # daemon can inspect it; never overwrite an active/apply path.
    from fwrouter_api.services.mihomo_config import _resolved_base_config_path
    import tempfile

    candidate_path: Path | None = None
    native_ok = False
    cleanup_ok = True
    try:
        parent = Path(_resolved_base_config_path()).parent
        if not parent.is_dir():
            return {"ok": False, "reason": "mihomo_preimage_native_invalid"}
        descriptor, raw_path = tempfile.mkstemp(prefix=".rollback-mihomo-preflight-", suffix=".yaml", dir=parent)
        candidate_path = Path(raw_path)
        try:
            descriptor_info = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_info.st_mode) or stat.S_IMODE(descriptor_info.st_mode) != 0o600:
                raise OSError("private Mihomo candidate descriptor is not a mode-0600 regular file")
            _write_all_fd(descriptor, config_bytes)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        from fwrouter_api.services.subscription_pipeline import validate_mihomo_candidate_config

        native = validate_mihomo_candidate_config(candidate_path=str(candidate_path))
        native_ok = bool(native.get("ok"))
    except Exception:
        native_ok = False
    finally:
        if candidate_path is not None:
            try:
                candidate_path.unlink(missing_ok=True)
            except OSError:
                cleanup_ok = False
    if not native_ok or not cleanup_ok:
        return {"ok": False, "reason": "mihomo_preimage_native_invalid"}
    return {"ok": True, "sha256": hashlib.sha256(config_bytes).hexdigest()}

def summarize_input_context(context: Any) -> dict[str, Any]:
    """Summarize staged inputs using hashes only; fingerprint payloads may contain secrets."""
    if not isinstance(context, tuple) or len(context) < 2:
        return {"available": False}
    fingerprint, base_inputs = context[0], context[1]
    if not isinstance(fingerprint, dict) or not isinstance(base_inputs, (list, tuple)):
        return {"available": False}
    from fwrouter_api.services.mihomo_reconcile_fingerprint import fingerprint_category_hashes

    try:
        category_hashes = fingerprint_category_hashes(fingerprint)
        fingerprint_hash = fingerprint.get("hash")
        fingerprint_version = fingerprint.get("version")
        normalized_base_inputs = sorted(
            (str(path), str(digest) if digest is not None else None)
            for path, digest in base_inputs
        )
    except (TypeError, ValueError):
        return {"available": False}
    base_digest = hashlib.sha256(
        json.dumps(normalized_base_inputs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "available": bool(isinstance(fingerprint_hash, str) and len(fingerprint_hash) == 64),
        "fingerprint_version": fingerprint_version,
        "fingerprint_sha256": fingerprint_hash,
        "category_sha256": category_hashes,
        "base_config_input_count": len(normalized_base_inputs),
        "base_config_inputs_sha256": base_digest,
    }

def log_input_context_change(*, stage: str, before: Any, after: Any) -> None:
    """Persist only category names, versions, and opaque hashes for fence diagnosis."""
    try:
        before_summary = _safe_mihomo_input_context_summary(before)
        after_summary = _safe_mihomo_input_context_summary(after)
        before_categories = before_summary.get("category_sha256", {})
        after_categories = after_summary.get("category_sha256", {})
        changed_categories = sorted(
            category for category in set(before_categories) | set(after_categories)
            if before_categories.get(category) != after_categories.get(category)
        )
        if before_summary.get("base_config_inputs_sha256") != after_summary.get("base_config_inputs_sha256"):
            changed_categories.append("base_config_inputs")
        write_technical_log(
            component="xray-subscription",
            event_type="mihomo_stage_input_context_changed",
            level="warning",
            message="Mihomo staged inputs changed during native validation.",
            details={
                "stage": stage,
                "changed_categories": changed_categories,
                "before": before_summary,
                "after": after_summary,
            },
            event_code="mihomo.stage_input_context_changed",
        )
    except Exception:
        # Diagnostics must not mask the original fail-closed generation result.
        pass

_safe_mihomo_input_context_summary = summarize_input_context
_log_mihomo_input_context_change = log_input_context_change
