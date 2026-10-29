"""Verified-generation evidence refresh owned by the controlled rollout.

This is a narrow adapter to the existing Xray generation owner. It never
publishes inventory, changes revisions, applies configuration or repairs a
runtime. The exact released lifecycle checkpoint and inherited owner flock
are prerequisites, not an alternative authentication or recovery mechanism.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import maintenance_lifecycle_admission as admission
from fwrouter_api.services import native_proof
from fwrouter_api.services import subscription
from fwrouter_api.services import xray_subscription_service as owner


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _checkpoint(state_root: Path, operation_id: str, manifest: str, expected: str) -> dict[str, Any]:
    admission._require_units_stopped()
    value = admission._read_checkpoint(state_root)
    path = state_root / "state" / "controlled-rollout" / "pending.json"
    raw = native_proof._read_regular_file(path, maximum_bytes=admission.MAX_CHECKPOINT_BYTES)
    if (hashlib.sha256(raw).hexdigest() != expected or json.loads(raw) != value
            or not isinstance(value, dict) or value.get("schema") != 4
            or value.get("status") != "pending" or value.get("operation_id") != operation_id
            or value.get("backend_manifest_sha256") != manifest
            or value.get("maintenance_operation") is not None):
        raise ValueError("ROLLOUT_NATIVE_CHECKPOINT_MISMATCH")
    runtime = value.get("runtime_quiescence")
    if (not admission._startup_runtime_quiescence_valid(runtime, operation_id)
            or runtime.get("phase") != "released" or runtime.get("unit_active_state") != "active"):
        raise ValueError("ROLLOUT_NATIVE_RUNTIME_NOT_RELEASED")
    transitions = value.get("source_transitions")
    if (not isinstance(transitions, list) or len(transitions) != 1
            or not isinstance(transitions[0], dict)
            or transitions[0].get("operation_id") != operation_id
            or transitions[0].get("from_manifest_sha256") != runtime["source_manifest_sha256"]
            or transitions[0].get("to_manifest_sha256") != manifest
            or value.get("initial_backend_manifest_sha256") != runtime["source_manifest_sha256"]):
        raise ValueError("ROLLOUT_NATIVE_SOURCE_TRANSITION_MISMATCH")
    if native_proof.backend_manifest_sha256(native_proof._backend_root()) != manifest:
        raise ValueError("ROLLOUT_NATIVE_INSTALLED_MANIFEST_MISMATCH")
    return value


def _verified_marker(digest: str, revision: int) -> dict[str, Any]:
    with db_session() as connection:
        state = subscription._read_subscription_native_generation(connection)
        row = connection.execute(
            "SELECT value_json FROM settings WHERE key=?", (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,),
        ).fetchone()
    if (row is None or state.get("status") != "verified"
            or state.get("inventory_digest") != digest or state.get("candidate_digest") != digest
            or state.get("verified_digest") != digest or state.get("candidate_revision") != revision
            or state.get("verified_revision") != revision or state.get("inventory_count", 0) <= 0):
        raise ValueError("ROLLOUT_NATIVE_VERIFIED_GENERATION_REQUIRED")
    marker = json.loads(row["value_json"])
    if not isinstance(marker, dict):
        raise ValueError("ROLLOUT_NATIVE_MARKER_INVALID")
    return marker


def refresh_rollout_native_evidence(
    *, expected_operation_id: str, expected_backend_manifest_sha256: str,
    expected_generation_digest: str, expected_generation_revision: int,
    expected_checkpoint_sha256: str, lifecycle_lock_fd: int, maintenance_lease_fd: int,
) -> dict[str, Any]:
    """Refresh evidence only for one released, exact verified-generation rollout.

    A failed or interrupted publication remains a failed owner operation; a
    retry repeats native proof and the existing verified-only CAS. No stale
    receipt can become success by replaying an HTTP response or operation ID.
    """
    if os.geteuid() != 0:
        return {"ok": False, "status": "blocked", "reason": "root_required"}
    try:
        operation_id = str(UUID(expected_operation_id))
        for value in (expected_backend_manifest_sha256, expected_generation_digest, expected_checkpoint_sha256):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("ROLLOUT_NATIVE_IDENTITY_INVALID")
        if type(expected_generation_revision) is not int or expected_generation_revision <= 0:
            raise ValueError("ROLLOUT_NATIVE_REVISION_INVALID")
        state_root = get_settings().paths.state_dir
        admission._verify_lifecycle_lock_fd(lifecycle_lock_fd, state_root)
        admission._verify_exclusive_lease_fd(maintenance_lease_fd, state_root)
        checkpoint = _checkpoint(state_root, operation_id, expected_backend_manifest_sha256,
                                 expected_checkpoint_sha256)
        with owner.xray_writer_guard(timeout_seconds=30.0):
            if owner.subscription_inventory_checkpoint_write_gate().get("ok") is not True:
                raise ValueError("ROLLOUT_NATIVE_GENERATION_CHECKPOINT_PENDING")
            before = _verified_marker(expected_generation_digest, expected_generation_revision)
            # Do not silently discard unknown marker extensions when the
            # established promotion owner reconstructs its canonical marker.
            if set(before) - {"schema", "status", "candidate_digest", "candidate_revision",
                              "verified_digest", "verified_revision", "reason", "inventory_count", "native_proof"}:
                raise ValueError("ROLLOUT_NATIVE_UNKNOWN_MARKER_FIELDS")
            source_fingerprint = owner._legacy_admission_source_fingerprint()
            adapter = owner._xray_adapter()
            proof = owner._collect_legacy_inventory_native_proof(
                adapter, expected_digest=expected_generation_digest, source_fingerprint=source_fingerprint,
                checkpoint_path=owner._xray_generation_checkpoint_path(adapter),
            )
            if proof.get("ok") is not True:
                raise ValueError("ROLLOUT_NATIVE_READBACK_INCOMPLETE")
            if (_checkpoint(state_root, operation_id, expected_backend_manifest_sha256,
                            expected_checkpoint_sha256) != checkpoint
                    or _verified_marker(expected_generation_digest, expected_generation_revision) != before):
                raise ValueError("ROLLOUT_NATIVE_PUBLICATION_FENCE_CHANGED")
            capability = subscription._claim_subscription_inventory_admission(
                allow_verified_proof_refresh=True, expected_digest=expected_generation_digest,
                expected_revision=expected_generation_revision,
            )
            if capability is None:
                raise ValueError("ROLLOUT_NATIVE_GENERATION_CAPABILITY_MISSING")
            # Preserve the proof-only native/intent fences in the capability;
            # the existing promotion owner rechecks them inside its DB commit.
            fence = owner._AdmissionFenceView(
                proof_only_source_fingerprint=str(proof["source_fingerprint"]),
                proof_only_core_runtime_fingerprint=str(proof["core_runtime_fingerprint"]),
                proof_only_selection_revision=int(proof["selection_revision"]),
                proof_only_selection_decision_id=proof.get("selection_decision_id"),
                proof_only_selection_snapshot=proof["selection_snapshot"],
                proof_only_routing_snapshot=proof["routing_snapshot"],
                proof_only_xray_incarnation=str(proof["xray_runtime_incarnation"]),
                proof_only_mihomo_incarnation=str(proof["mihomo_runtime_incarnation"]),
                proof_only_xray_config_sha256=str(proof["xray_config_sha256"]),
                proof_only_mihomo_config_sha256=str(proof["mihomo_config_sha256"]),
                proof_only_mihomo_fingerprint_hash=str(proof["mihomo_fingerprint_hash"]),
                proof_only_core_manifest_hash=str(proof["core_dataplane_proof"]["manifest_hash"]),
                proof_only_core_apply_id=str(proof["core_dataplane_proof"]["apply_id"]),
                proof_only_core_enforcement_level=str(proof["core_dataplane_proof"]["enforcement_level"]),
                proof_only_core_live_mode=str(proof["core_dataplane_proof"]["live_global_mode"]),
            )
            from fwrouter_api.services.mihomo_reconcile_fingerprint import _state_path
            receipt_path = state_root / "generated" / "mihomo" / "reconcile-state.json"
            if _state_path() != receipt_path:
                raise ValueError("ROLLOUT_NATIVE_RECEIPT_PATH_UNSUPPORTED")
            receipt_before = hashlib.sha256(native_proof._read_regular_file(
                receipt_path, maximum_bytes=2 * 1024 * 1024)).hexdigest()

            def still_owned(fingerprint_hash: str) -> bool:
                return (
                    fingerprint_hash == proof["mihomo_fingerprint_hash"]
                    and _checkpoint(state_root, operation_id, expected_backend_manifest_sha256,
                                    expected_checkpoint_sha256) == checkpoint
                    and _verified_marker(expected_generation_digest, expected_generation_revision) == before
                    and owner._legacy_admission_source_fingerprint() == source_fingerprint
                    and owner._legacy_admission_runtime_fences_match(fence)
                )

            if not owner._publish_mihomo_udp_receipt_with_owner(
                expected_mihomo_runtime=proof["mihomo_runtime_incarnation"],
                expected_mihomo_sha=proof["mihomo_config_sha256"],
                reconcile_reason="controlled_rollout_native_evidence", operation_id=operation_id,
                routing_snapshot=proof["routing_snapshot"], owner_matches=still_owned,
            ):
                raise ValueError("ROLLOUT_NATIVE_MIHOMO_RECEIPT_FAILED")
            receipt_after = hashlib.sha256(native_proof._read_regular_file(
                receipt_path, maximum_bytes=2 * 1024 * 1024)).hexdigest()
            capability = replace(capability, **fence.__dict__)
            if _checkpoint(state_root, operation_id, expected_backend_manifest_sha256,
                           expected_checkpoint_sha256) != checkpoint:
                raise ValueError("ROLLOUT_NATIVE_CHECKPOINT_CHANGED_BEFORE_COMMIT")
            result = subscription._promote_subscription_inventory_generation(capability)
            if result.get("ok") is not True:
                raise ValueError("ROLLOUT_NATIVE_GENERATION_REFRESH_FAILED")
            after = _verified_marker(expected_generation_digest, expected_generation_revision)
            if ({key: value for key, value in before.items() if key != "native_proof"}
                    != {key: value for key, value in after.items() if key != "native_proof"}
                    or not isinstance(after.get("native_proof"), dict)):
                raise ValueError("ROLLOUT_NATIVE_INTENT_CHANGED")
            if _checkpoint(state_root, operation_id, expected_backend_manifest_sha256,
                           expected_checkpoint_sha256) != checkpoint:
                raise ValueError("ROLLOUT_NATIVE_CHECKPOINT_CHANGED_AFTER_COMMIT")
            return {"ok": True, "status": "verified", "operation_id": operation_id,
                    "generation_digest": expected_generation_digest, "generation_revision": expected_generation_revision,
                    "checkpoint_sha256": expected_checkpoint_sha256,
                    "marker_before_sha256": hashlib.sha256(_canonical(before)).hexdigest(),
                    "marker_after_sha256": hashlib.sha256(_canonical(after)).hexdigest(),
                    "native_proof_sha256": hashlib.sha256(_canonical(after["native_proof"])).hexdigest(),
                    "owner_artifact_transition": {
                        "relative_path": "generated/mihomo/reconcile-state.json",
                        "before_sha256": receipt_before, "after_sha256": receipt_after,
                    },
                    "evidence_only": True, "runtime_apply_performed": False}
    except Exception:
        # No underlying diagnostic or potentially sensitive payload escapes.
        return {"ok": False, "status": "blocked", "reason": "rollout_native_evidence_unverified"}
