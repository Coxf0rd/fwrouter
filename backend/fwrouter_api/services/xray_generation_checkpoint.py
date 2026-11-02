"""Read-only Xray generation checkpoint contracts and immutable admission data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _xray_generation_checkpoint_path(adapter: Any) -> Path:
    from fwrouter_api.adapters.xray_common import _default_xray_config_path

    configured_path = getattr(adapter, "config_path", None) or _default_xray_config_path()
    return Path(configured_path).parent / ".generation" / "generation-checkpoint.json"


def _legacy_admission_checkpoint_payload(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        parent = path.parent.lstat()
        if (
            not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or info.st_size > 65536
            or not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            return None
        raw = path.read_bytes()
        after = path.lstat()
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino) or len(raw) > 65536:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    allowed = {
        "schema", "operation_mode", "operation_id", "phase", "require_native_proof", "candidate_digest",
        "candidate_revision", "source_fingerprint", "selection_revision",
        "selection_decision_id", "created_at", "proof_summary",
        "xray_runtime_incarnation", "mihomo_runtime_incarnation",
        "xray_config_sha256", "mihomo_config_sha256", "mihomo_fingerprint_hash",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) - allowed
        or type(payload.get("schema")) is not int
        or payload.get("schema") != 1
        or payload.get("operation_mode") != "proof_only_legacy_admission"
        or payload.get("phase") not in {"proof_started", "proof_complete", "verified"}
        or ("require_native_proof" in payload and type(payload.get("require_native_proof")) is not bool)
        or not isinstance(payload.get("operation_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", payload.get("operation_id", ""))
        or not isinstance(payload.get("candidate_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload.get("candidate_digest", ""))
        or type(payload.get("candidate_revision")) is not int
        or payload.get("candidate_revision") < 1
        or not isinstance(payload.get("source_fingerprint"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload.get("source_fingerprint", ""))
        or type(payload.get("selection_revision")) is not int
    ):
        return None
    return payload


def _checkpoint_operation_mode(checkpoint: Any) -> str | None:
    """Return a supported checkpoint mode, rejecting unknown future modes."""
    if not isinstance(checkpoint, dict):
        raise ValueError("XRAY_GENERATION_CHECKPOINT_INVALID")
    mode = checkpoint.get("operation_mode")
    if mode is None:
        return None  # Historical apply/recovery checkpoints predate this field.
    if mode != "proof_only_legacy_admission":
        raise ValueError("XRAY_GENERATION_CHECKPOINT_MODE_UNSUPPORTED")
    return str(mode)


def _checkpoint_selection_for_recovery(
    checkpoint: dict[str, Any], current_selection: dict[str, Any],
) -> dict[str, Any] | None:
    """Use checkpoint-bound intent when current Core has no VPN target selected."""
    routing = current_selection.get("routing") if isinstance(current_selection.get("routing"), dict) else {}
    mode = str(routing.get("server_mode") or "auto").strip().lower()
    target = (
        routing.get("desired_fixed_server_id") or routing.get("applied_fixed_server_id")
        if mode == "fixed" else routing.get("active_auto_server_id")
    )
    if str(target or "").strip():
        return current_selection
    checkpoint_selection = checkpoint.get("auto_selection_after")
    if not isinstance(checkpoint_selection, dict):
        checkpoint_selection = checkpoint.get("auto_selection_before")
    return checkpoint_selection if isinstance(checkpoint_selection, dict) else None


@dataclass(frozen=True)
class _AdmissionFenceView:
    """Narrow value object for reusing the native runtime fence verifier."""

    proof_only_source_fingerprint: str
    proof_only_core_runtime_fingerprint: str
    proof_only_selection_revision: int
    proof_only_selection_decision_id: str | None
    proof_only_selection_snapshot: dict[str, Any]
    proof_only_routing_snapshot: dict[str, Any]
    proof_only_xray_incarnation: str
    proof_only_mihomo_incarnation: str
    proof_only_xray_config_sha256: str
    proof_only_mihomo_config_sha256: str
    proof_only_mihomo_fingerprint_hash: str
    proof_only_core_manifest_hash: str
    proof_only_core_apply_id: str
    proof_only_core_enforcement_level: str
    proof_only_core_live_mode: str


def build_native_validation_proof(staged_generation: dict[str, Any]) -> dict[str, Any]:
    """Project staged native validation receipts into the checkpoint proof."""
    native_receipts = staged_generation.get("native_validation") or {}
    xray_receipt = native_receipts.get("xray") or {}
    mihomo_receipts: dict[str, Any] = {}
    for name in ("transition", "final"):
        candidate = native_receipts.get(name) or {}
        validation = candidate.get("validation") or {}
        receipt_bytes = json.dumps(
            validation, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")
        mihomo_receipts[name] = {
            "ok": validation.get("ok") is True,
            "candidate_sha256": candidate.get("candidate_sha256"),
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        }
    identity_receipt_bytes = json.dumps(
        xray_receipt.get("expected_client_identities") or [],
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return {
        "xray": {
            "ok": xray_receipt.get("stage") == "validated",
            "candidate_sha256": xray_receipt.get("candidate_sha256"),
            "expected_client_identities_sha256": hashlib.sha256(identity_receipt_bytes).hexdigest(),
        },
        "mihomo": mihomo_receipts,
    }


__all__ = [
    "_AdmissionFenceView",
    "_checkpoint_operation_mode",
    "_checkpoint_selection_for_recovery",
    "_legacy_admission_checkpoint_payload",
    "_xray_generation_checkpoint_path",
    "build_native_validation_proof",
]
