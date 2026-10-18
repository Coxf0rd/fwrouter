from __future__ import annotations

import filecmp
import hashlib
import inspect
import math
import os
import shutil as shutil
import yaml
from uuid import uuid4
from pathlib import Path
from typing import Any
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

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
    """Defer changed defaults to the canonical verified reconcile path.

    This compatibility entry point deliberately performs no runtime or state
    mutation. The fast patch path cannot publish a native proof receipt, so a
    caller must use the full reconcile before applying a changed default.
    """
    del routing
    return {
        "ok": False,
        "job_id": job_id,
        "reconcile_action": "none",
        "reconcile_reason": "verified_publication_requires_full_reconcile",
        "fast_path": True,
        "fallback_required": True,
    }


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


MAX_SEMANTIC_YAML_NODES = 100_000
MAX_SEMANTIC_YAML_DEPTH = 100
MAX_SEMANTIC_CONFIG_BYTES = 1024 * 1024


def _yaml_tree_is_unambiguous(
    node: Any,
    ancestors: set[int] | None = None,
    seen: set[int] | None = None,
    budget: list[int] | None = None,
    depth: int = 0,
) -> bool:
    """Reject YAML constructs whose parsed meaning or expansion is unsafe to compare."""
    if node is None:
        return True
    active = ancestors if ancestors is not None else set()
    visited = seen if seen is not None else set()
    visits = budget if budget is not None else [0]
    visits[0] += 1
    if visits[0] > MAX_SEMANTIC_YAML_NODES or depth > MAX_SEMANTIC_YAML_DEPTH:
        return False
    identity = id(node)
    # YAML aliases share node identities. Reject all aliases (including cycles)
    # before construction, since expanding an acyclic alias can multiply memory
    # exponentially even when the source text and node count are small.
    if identity in active or identity in visited:
        return False
    active.add(identity)
    visited.add(identity)
    try:
        if isinstance(node, MappingNode):
            keys: set[str] = set()
            for key_node, value_node in node.value:
                if not isinstance(key_node, ScalarNode) or key_node.tag != "tag:yaml.org,2002:str":
                    return False
                if key_node.value in keys or not _yaml_tree_is_unambiguous(
                    value_node, active, visited, visits, depth + 1,
                ):
                    return False
                keys.add(key_node.value)
                if not _yaml_tree_is_unambiguous(key_node, active, visited, visits, depth + 1):
                    return False
            return True
        if isinstance(node, SequenceNode):
            return all(
                _yaml_tree_is_unambiguous(item, active, visited, visits, depth + 1)
                for item in node.value
            )
        return isinstance(node, ScalarNode)
    finally:
        active.remove(identity)


def _yaml_value_is_json_safe(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_yaml_value_is_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _yaml_value_is_json_safe(item)
            for key, item in value.items()
        )
    return False


def _semantic_config_files_equal(active_path: str, candidate_path: str) -> bool:
    """Compare only bounded, unambiguous YAML; every uncertain case falls back to apply."""
    from fwrouter_api.services.mihomo_config_paths import MAX_BASE_CONFIG_BYTES

    parsed: list[dict[str, Any]] = []
    loader_base = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    for path in (active_path, candidate_path):
        try:
            size_before = os.path.getsize(path)
            if size_before > min(MAX_BASE_CONFIG_BYTES, MAX_SEMANTIC_CONFIG_BYTES):
                return False
            with open(path, "rb") as handle:
                raw = handle.read(min(MAX_BASE_CONFIG_BYTES, MAX_SEMANTIC_CONFIG_BYTES) + 1)
            if len(raw) > min(MAX_BASE_CONFIG_BYTES, MAX_SEMANTIC_CONFIG_BYTES):
                return False
            text = raw.decode("utf-8")
            yaml_root = yaml.compose(text, Loader=loader_base)
            if not _yaml_tree_is_unambiguous(yaml_root):
                return False
            del yaml_root
            value = yaml.load(text, Loader=loader_base)
            if not isinstance(value, dict) or not _yaml_value_is_json_safe(value):
                return False
            parsed.append(value)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
            return False
    try:
        return config._configs_equal(parsed[0], parsed[1])
    except (TypeError, ValueError):
        return False


def promote_mihomo_candidate_config(
    *, selection_fenced: bool = False, expected_selection_revision: int | None = None,
    _inventory_admission: Any = None,
    _candidate_input_fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not selection_fenced:
        with xray_writer_guard(timeout_seconds=30.0):
            blocked = _inventory_candidate_promotion_blocked(_inventory_admission)
            if blocked is not None:
                return blocked
            candidate_fingerprint = _candidate_input_fingerprint
            if _inventory_admission is None:
                if not isinstance(candidate_fingerprint, dict):
                    from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint
                    candidate_fingerprint = current_mihomo_input_fingerprint()
                candidate_blocked = _manual_candidate_provenance_blocked(candidate_fingerprint)
                if candidate_blocked is not None:
                    return candidate_blocked
            with db_session() as connection:
                revision = read_selection_revision(connection)
                next_revision = advance_selection_revision(connection, expected_revision=revision)
            if next_revision is None:
                return {"ok": False, "promoted": False, "error_code": "VPN_AUTO_SELECTION_REVISION_CONFLICT"}
            result = _promote_mihomo_candidate_config_under_fence(
                inventory_admission=_inventory_admission,
                candidate_input_fingerprint=(
                    candidate_fingerprint if _inventory_admission is None else None
                ),
            )
            return {**result, "selection_revision": next_revision}
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    if not xray_writer_guard_is_held() or type(expected_selection_revision) is not int:
        return {"ok": False, "promoted": False, "error_code": "VPN_AUTO_SELECTION_FENCE_REQUIRED"}
    with db_session() as connection:
        if read_selection_revision(connection) != expected_selection_revision:
            return {"ok": False, "promoted": False, "error_code": "VPN_AUTO_SELECTION_REVISION_CONFLICT"}
    blocked = _inventory_candidate_promotion_blocked(_inventory_admission)
    if blocked is not None:
        return blocked
    if _inventory_admission is None:
        candidate_fingerprint = _candidate_input_fingerprint
        if not isinstance(candidate_fingerprint, dict):
            from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint
            candidate_fingerprint = current_mihomo_input_fingerprint()
        candidate_blocked = _manual_candidate_provenance_blocked(candidate_fingerprint)
        if candidate_blocked is not None:
            return candidate_blocked
    else:
        candidate_fingerprint = None
    return _promote_mihomo_candidate_config_under_fence(
        inventory_admission=_inventory_admission,
        candidate_input_fingerprint=(
            candidate_fingerprint if _inventory_admission is None else None
        ),
    )


def _manual_candidate_provenance_blocked(
    expected_fingerprint: dict[str, Any] | None,
) -> dict[str, Any] | None:
    from fwrouter_api.services.mihomo_reconcile_fingerprint import (
        candidate_provenance_matches, current_mihomo_input_fingerprint,
    )

    try:
        expected = expected_fingerprint if isinstance(expected_fingerprint, dict) else current_mihomo_input_fingerprint()
        payload = expected.get("payload") if isinstance(expected.get("payload"), dict) else {}
        current = current_mihomo_input_fingerprint(payload.get("routing"))
        expected_hash = expected.get("hash")
        if not isinstance(expected_hash, str) or current.get("hash") != expected_hash:
            raise ValueError("candidate_inputs_changed")
        if not candidate_provenance_matches(expected_fingerprint_hash=expected_hash):
            raise ValueError("candidate_provenance_mismatch")
    except Exception as exc:
        reason = str(exc) if str(exc) in {"candidate_inputs_changed", "candidate_provenance_mismatch"} else "candidate_provenance_unavailable"
        return {
            "ok": False, "promoted": False,
            "stage": "candidate_provenance",
            "error_code": "MIHOMO_CANDIDATE_PROVENANCE_UNVERIFIED",
            "reason": reason,
            "last_good_retained": True,
        }
    return None


def _inventory_candidate_promotion_blocked(admission: Any) -> dict[str, Any] | None:
    from fwrouter_api.services.subscription import _subscription_inventory_generation_gate

    gate = _subscription_inventory_generation_gate(admission, require_writer_guard=True)
    if gate.get("ok"):
        return None
    return {
        "ok": False,
        "promoted": False,
        "stage": "inventory_generation_admission",
        "error_code": "SUBSCRIPTION_INVENTORY_GENERATION_UNVERIFIED",
        "reason": gate.get("reason"),
        "last_good_retained": True,
    }


def _promote_mihomo_candidate_config_under_fence(
    *, inventory_admission: Any = None,
    candidate_input_fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocked = _inventory_candidate_promotion_blocked(inventory_admission)
    if blocked is not None:
        return blocked
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

    try:
        candidate_bytes = Path(candidate_path).read_bytes()
        candidate_hash = _file_hash(candidate_path)
        if not candidate_hash or hashlib.sha256(candidate_bytes).hexdigest() != candidate_hash:
            return {"ok": False, "promoted": False, "error_code": "MIHOMO_CANDIDATE_CHANGED_DURING_PROMOTE",
                    "last_good_retained": True}
        if inventory_admission is None:
            from fwrouter_api.services.mihomo_reconcile_fingerprint import (
                candidate_provenance_matches, current_mihomo_input_fingerprint,
            )
            expected = candidate_input_fingerprint if isinstance(candidate_input_fingerprint, dict) else {}
            expected_hash = expected.get("hash")
            payload = expected.get("payload") if isinstance(expected.get("payload"), dict) else {}
            current = current_mihomo_input_fingerprint(payload.get("routing"))
            if (not isinstance(expected_hash, str) or current.get("hash") != expected_hash
                    or not candidate_provenance_matches(
                        expected_fingerprint_hash=expected_hash, candidate_bytes=candidate_bytes,
                    )):
                return {"ok": False, "promoted": False,
                        "error_code": "MIHOMO_CANDIDATE_PROVENANCE_UNVERIFIED",
                        "last_good_retained": True}
        os.makedirs(os.path.dirname(base_path), exist_ok=True)
        atomic_write_text(Path(base_path), candidate_bytes.decode("utf-8"))
        if _file_hash(base_path) != candidate_hash:
            return {"ok": False, "promoted": False, "error_code": "MIHOMO_CANDIDATE_COPY_MISMATCH",
                    "last_good_retained": True}
    except (OSError, UnicodeError):
        return {"ok": False, "promoted": False, "error_code": "MIHOMO_CANDIDATE_READ_FAILED",
                "last_good_retained": True}

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


def _capture_mihomo_reconcile_checkpoint(
    base_path: str, candidate_path: str, *, routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    try:
        checkpoint_text = original.decode("utf-8")
    except UnicodeDecodeError:
        checkpoint_text = ""
    intent = routing if isinstance(routing, dict) else {}
    mode = str(intent.get("applied_mode") or intent.get("desired_mode") or "direct").strip().lower()
    required_vpn_policy = (
        mode == "vpn"
        or str(intent.get("selective_default") or "direct").strip().lower() == "vpn"
        or _config_has_mandatory_vpn_routes(checkpoint_text, routing=intent)
    )
    return {
        "ok": True,
        "base_path": str(base),
        "backup_path": str(backup),
        "before_hash": old_hash,
        "candidate_hash": _file_hash(candidate_path),
        "before_udp_policy_safe": _config_has_failclosed_udp_policy(checkpoint_text),
        "before_mandatory_vpn_policy_required": required_vpn_policy,
    }


def _config_has_failclosed_udp_policy(config_text: str) -> bool:
    try:
        loaded = yaml.load(config_text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(loaded, dict):
        return False
    groups = {
        str(item.get("name") or ""): item
        for item in (loaded.get("proxy-groups") or [])
        if isinstance(item, dict)
    }
    owned = [name for name in groups if name in {"vpn-auto", "vpn-global"} or name.startswith("fwrouter-subject-")]
    if not {"vpn-auto", "vpn-global"} <= set(owned):
        return False
    for name in owned:
        targets = groups[name].get("proxies")
        if not isinstance(targets, list) or not targets or targets[-1] != "REJECT" or "DIRECT" in targets:
            return False
    from fwrouter_api.services.mihomo_config_rules import _mandatory_vpn_udp_guard

    sub_rules = loaded.get("sub-rules") if isinstance(loaded.get("sub-rules"), dict) else {}
    sequences = [loaded.get("rules"), sub_rules.get("fwrouter-transparent"), sub_rules.get("fwrouter-full-vpn")]
    for rules in sequences:
        if not isinstance(rules, list) or not rules:
            return False
        for index, value in enumerate(rules):
            guard = _mandatory_vpn_udp_guard(str(value))
            if guard and (index + 1 >= len(rules) or str(rules[index + 1]) != guard):
                return False
    full = sequences[-1]
    return len(full) >= 2 and str(full[-2]) == "MATCH,vpn-global" and str(full[-1]) == "NETWORK,udp,REJECT"


def _config_has_mandatory_vpn_routes(
    config_text: str, *, routing: dict[str, Any] | None = None,
) -> bool:
    try:
        loaded = yaml.load(config_text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    except yaml.YAMLError:
        return True
    if not isinstance(loaded, dict):
        return True
    from fwrouter_api.services.mihomo_config_rules import _mandatory_vpn_udp_guard

    sub_rules = loaded.get("sub-rules") if isinstance(loaded.get("sub-rules"), dict) else {}
    intent = routing if isinstance(routing, dict) else {}
    mode = str(intent.get("applied_mode") or intent.get("desired_mode") or "direct").strip().lower()
    sequences = [loaded.get("rules"), sub_rules.get("fwrouter-transparent")]
    full_rules = sub_rules.get("fwrouter-full-vpn")
    if isinstance(full_rules, list):
        # Full-VPN fallback is only reachable when the current Core policy
        # sends traffic into the full-VPN contour. Subject-scoped entries are
        # independently reachable and remain mandatory in every mode.
        if any("fwrouter-subject-" in str(rule) for rule in full_rules):
            sequences.append(full_rules)
        elif mode == "vpn" or str(intent.get("selective_default") or "direct").strip().lower() == "vpn":
            sequences.append(full_rules)
    for sequence in sequences:
        if isinstance(sequence, list) and any(_mandatory_vpn_udp_guard(str(rule)) for rule in sequence):
            return True
    return False


def _checkpoint_rollback_policy_allowed(checkpoint: dict[str, Any]) -> bool:
    return not (
        checkpoint.get("before_mandatory_vpn_policy_required") is True
        and checkpoint.get("before_udp_policy_safe") is not True
    )


def _mihomo_incarnation() -> str | None:
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation

    value = get_mihomo_runtime_incarnation()
    return str(value).strip() if value else None


def _native_udp_policy_proof() -> dict[str, Any]:
    """Read the loaded Mihomo rules and selectors before publishing a receipt."""
    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER

    health = DEFAULT_MIHOMO_ADAPTER.health()
    details = health.details if isinstance(health.details, dict) else {}
    proof = details.get("vpn_udp_policy") if isinstance(details.get("vpn_udp_policy"), dict) else {}
    return {
        "ok": str(getattr(health.runtime_state, "value", health.runtime_state)) == "running"
        and proof.get("wiring_proven") is True
        and proof.get("native_top_level_rules_match") is True,
        "native_top_level_rules_match": proof.get("native_top_level_rules_match") is True,
        "selector_membership_matches": proof.get("selector_membership_matches") is True,
        "guarded_rules_match": proof.get("guarded_rules_match") is True,
    }


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
    if not _checkpoint_rollback_policy_allowed(checkpoint):
        return {"ok": False, "recovered": False, "reason": "previous_config_not_failclosed_for_vpn_udp"}
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
    _inventory_admission: Any = None,
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

    from fwrouter_api.services.subscription import _subscription_inventory_generation_gate
    inventory_gate = _subscription_inventory_generation_gate(_inventory_admission)
    if not inventory_gate.get("ok"):
        return {
            "ok": False,
            "stage": "inventory_generation_admission",
            "error_code": "SUBSCRIPTION_INVENTORY_GENERATION_UNVERIFIED",
            "reconcile_action": "none",
            "reconcile_reason": str(inventory_gate.get("reason") or "generation_unverified"),
            "candidate": None,
            "promoted": {"ok": False, "promoted": False, "reason": "inventory_generation_unverified"},
            "container": {"ok": False, "action": "none", "reason": "inventory_generation_unverified"},
            "last_good_retained": True,
            "inventory_generation": {
                "status": inventory_gate.get("status"),
                "candidate_digest": inventory_gate.get("candidate_digest"),
                "candidate_revision": inventory_gate.get("candidate_revision"),
            },
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
            native_proof = _native_udp_policy_proof()
            if not native_proof.get("ok") or not _selection_publication_still_owned(
                operation_id=operation_id, expected_revision=owned_revision,
                expected_incarnation=runtime_incarnation_before,
                expected_input_hash=(input_fingerprint or {}).get("hash"),
                expected_active_hash=active_config_hash_before,
                routing=routing_dict, verification=verification,
            ):
                return {"ok": False, "stage": "native_udp_policy_readback",
                        "reconcile_reason": "native_policy_or_fence_unconfirmed",
                        "verification_callback_result": verification,
                        "native_udp_policy_proof": native_proof,
                        "last_good_retained": True}
            if input_fingerprint is not None and not write_mihomo_reconcile_fingerprint_state(
                fingerprint=input_fingerprint,
                result={"ok": True, "reconcile_reason": "input_fingerprint_unchanged"},
                native_proof=native_proof,
                expected_active_hash=active_config_hash_before,
                expected_runtime_incarnation=runtime_incarnation_before,
            ):
                return {"ok": False, "stage": "native_udp_policy_receipt",
                        "reconcile_reason": "receipt_fence_mismatch",
                        "last_good_retained": True}
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
        if not files_match:
            files_match = _semantic_config_files_equal(base_path, candidate_path)
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
                inventory_block = _inventory_candidate_promotion_blocked(_inventory_admission)
                if inventory_block is not None:
                    result.update({
                        **inventory_block,
                        "stage": "inventory_generation_publication_revalidation",
                        "reconcile_action": "none",
                        "reconcile_reason": "inventory_generation_stale",
                    })
                    return result
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
                else:
                    native_proof = _native_udp_policy_proof()
                    if not native_proof.get("ok") or not _selection_publication_still_owned(
                        operation_id=operation_id, expected_revision=owned_revision,
                        expected_incarnation=runtime_incarnation_before,
                        expected_input_hash=(input_fingerprint or {}).get("hash"),
                        expected_active_hash=active_config_hash_before, routing=routing_dict,
                        verification=verification,
                    ):
                        result.update({"ok": False, "stage": "native_udp_policy_readback",
                                       "reconcile_reason": "native_policy_or_fence_unconfirmed",
                                       "native_udp_policy_proof": native_proof,
                                       "last_good_retained": True})
                    elif input_fingerprint is not None:
                        receipt_written = write_mihomo_reconcile_fingerprint_state(
                            fingerprint=input_fingerprint, result=result, native_proof=native_proof,
                            expected_active_hash=active_config_hash_before,
                            expected_runtime_incarnation=runtime_incarnation_before,
                        )
                        if not receipt_written:
                            result.update({"ok": False, "stage": "native_udp_policy_receipt",
                                           "reconcile_reason": "receipt_fence_mismatch",
                                           "last_good_retained": True})
        return result

    restart_action = "force_recreate"

    with xray_writer_guard(timeout_seconds=30.0):
        inventory_block = _inventory_candidate_promotion_blocked(_inventory_admission)
        if inventory_block is not None:
            return {
                **inventory_block,
                "stage": "inventory_generation_publication_revalidation",
                "reconcile_action": "none",
                "reconcile_reason": "inventory_generation_stale",
            }
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
            _capture_mihomo_reconcile_checkpoint(base_path, candidate_path, routing=routing_dict)
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
            _inventory_admission=_inventory_admission,
            _candidate_input_fingerprint=input_fingerprint,
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
    native_proof: dict[str, Any] = {}
    receipt_written = False
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
                native_proof = _native_udp_policy_proof()
                publication_owned = bool(native_proof.get("ok")) and _selection_publication_still_owned(
                    operation_id=operation_id,
                    expected_revision=recovery_expected_revision,
                    expected_incarnation=runtime_incarnation_after,
                    expected_input_hash=(input_fingerprint or {}).get("hash"),
                    expected_active_hash=prepared_candidate_hash,
                    routing=routing_dict,
                    verification=verification,
                )
            if publication_owned and input_fingerprint is not None:
                receipt_written = write_mihomo_reconcile_fingerprint_state(
                    fingerprint=input_fingerprint,
                    result={"ok": True, "reconcile_reason": "structural_change", "operation_id": operation_id},
                    native_proof=native_proof,
                    expected_active_hash=prepared_candidate_hash,
                    expected_runtime_incarnation=runtime_incarnation_after,
                )
                publication_owned = publication_owned and receipt_written
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
        "operation_id": operation_id,
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
    if verified and publication_owned and receipt_written and input_fingerprint is not None:
        # This receipt identifies the exact selection-fence revision advanced by
        # this reconcile operation, after native proof and fingerprint publication.
        # Consumers may acknowledge only this operation-owned CAS transition;
        # they must continue rejecting any other changed selection context.
        result["reentry_generation_fence"] = {
            "schema": "mihomo-reentry-generation-fence/v1",
            "operation_id": operation_id,
            "job_id": job_id,
            "before_revision": expected_selection_revision,
            "selection_revision": recovery_expected_revision,
            "runtime_incarnation": runtime_incarnation_after,
            "active_config_hash": prepared_candidate_hash,
            "native_proof_verified": native_proof.get("ok") is True,
            "publication_verified": True,
        }
    log_result = {key: value for key, value in result.items()
                  if key not in {"operation_id", "reentry_generation_fence"}}
    config._write_mihomo_reconcile_logs(
        ok=bool(result["ok"]),
        event_type="mihomo_reconciled" if result["ok"] else "mihomo_reconcile_failed",
        operational_level="info" if result["ok"] else "warning",
        technical_level="info" if result["ok"] else "warning",
        message="Mihomo runtime reconciled." if result["ok"] else "Mihomo runtime reconcile failed after promote/restart.",
        details=log_result,
    )
    return result
