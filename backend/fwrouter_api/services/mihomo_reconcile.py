from __future__ import annotations

import filecmp
import hashlib
import inspect
import math
import os
import stat
import shutil as shutil
import tempfile
import yaml
from pathlib import Path
from uuid import UUID, uuid4
from typing import Any
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from fwrouter_api.services import mihomo_config as config
from fwrouter_api.services.artifacts import atomic_write_text
from fwrouter_api.services.mihomo_config_yaml import (
    MAX_MIHOMO_CONFIG_BYTES,
    MAX_MIHOMO_YAML_DEPTH,
    MAX_MIHOMO_YAML_NODES,
    MihomoYamlError,
    load_mihomo_yaml_mapping,
)
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


MAX_SEMANTIC_YAML_NODES = MAX_MIHOMO_YAML_NODES
MAX_SEMANTIC_YAML_DEPTH = MAX_MIHOMO_YAML_DEPTH
MAX_SEMANTIC_CONFIG_BYTES = MAX_MIHOMO_CONFIG_BYTES


def _fixed_generation_checkpoint_path(operation_id: str) -> Path | None:
    """Resolve an operation-keyed checkpoint only inside the protected state dir."""
    try:
        canonical_id = str(UUID(operation_id))
    except (TypeError, ValueError, AttributeError):
        return None
    if canonical_id != operation_id:
        return None
    directory = config._resolved_last_good_mihomo_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            return None
    except OSError:
        return None
    return directory / f"fixed-generation-{canonical_id}.yaml"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_fixed_generation_checkpoint(
    operation_id: str, writer: Any,
) -> dict[str, Any] | None:
    """Stream immutable checkpoint bytes into a protected, no-replace artifact."""
    path = _fixed_generation_checkpoint_path(operation_id)
    if path is None:
        return None
    if path.exists() or path.is_symlink():
        return None
    temporary: Path | None = None
    digest = hashlib.sha256()
    size = 0
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            while True:
                chunk = writer()
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    try:
                        written = os.write(descriptor, view)
                    except InterruptedError:
                        continue
                    if written <= 0:
                        raise OSError("short write while publishing fixed-generation checkpoint")
                    view = view[written:]
                digest.update(chunk)
                size += len(chunk)
            if size <= 0:
                return None
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(str(temporary), str(path))
        os.unlink(str(temporary))
        temporary = None
        _fsync_directory(path.parent)
        return {"path": str(path), "sha256": digest.hexdigest(), "size": size}
    except OSError:
        return None
    finally:
        if temporary is not None:
            try:
                os.unlink(str(temporary))
            except OSError:
                pass


def _write_fixed_generation_checkpoint(operation_id: str, data: bytes) -> dict[str, Any] | None:
    """Write immutable checkpoint bytes before DB reservation, without overwrite."""
    if not data:
        return None
    path = _fixed_generation_checkpoint_path(operation_id)
    if path is None:
        return None
    digest = hashlib.sha256(data).hexdigest()
    if path.exists() or path.is_symlink():
        try:
            info = path.lstat()
            if (
                stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size == len(data) and _file_hash(str(path)) == digest
            ):
                return {"path": str(path), "sha256": digest, "size": len(data)}
        except OSError:
            pass
        return None
    chunks = iter((data, b""))
    return _publish_fixed_generation_checkpoint(operation_id, lambda: next(chunks))


def _fixed_generation_checkpoint_matches(operation_id: str, reference: Any) -> bool:
    if not isinstance(reference, dict):
        return False
    path = _fixed_generation_checkpoint_path(operation_id)
    if path is None or reference.get("path") != str(path):
        return False
    try:
        info = path.lstat()
        return bool(
            stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
            and type(reference.get("size")) is int
            and reference["size"] > 0
            and path.stat().st_size == reference["size"]
            and isinstance(reference.get("sha256"), str)
            and _file_hash(str(path)) == reference["sha256"]
        )
    except OSError:
        return False


def _cleanup_fixed_generation_checkpoint(operation_id: str, reference: Any) -> bool:
    """Remove only the exact operation-owned checkpoint after final completion."""
    if not _fixed_generation_checkpoint_matches(operation_id, reference):
        return False
    path = _fixed_generation_checkpoint_path(operation_id)
    if path is None:
        return False
    try:
        path.unlink()
        _fsync_directory(path.parent)
        return True
    except OSError:
        return False


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
    parsed: list[dict[str, Any]] = []
    for path in (active_path, candidate_path):
        try:
            parsed.append(load_mihomo_yaml_mapping(path, max_bytes=MAX_SEMANTIC_CONFIG_BYTES))
        except (MihomoYamlError, OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
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
    operation_id: str | None = None,
) -> dict[str, Any]:
    base = Path(base_path)
    backup = (
        _fixed_generation_checkpoint_path(operation_id)
        if operation_id is not None else config._resolved_last_good_mihomo_dir() / "config.previous.yaml"
    )
    if backup is None:
        return {"ok": False, "reason": "checkpoint_operation_invalid", "base_path": str(base)}
    if not base.is_file():
        return {"ok": False, "reason": "no_active_config", "base_path": str(base), "backup_path": str(backup)}
    try:
        if operation_id is not None:
            before = base.lstat()
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                return {"ok": False, "reason": "no_active_config", "base_path": str(base), "backup_path": str(backup)}
            source_fd = os.open(base, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(source_fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    return {"ok": False, "reason": "checkpoint_source_changed", "base_path": str(base), "backup_path": str(backup)}
                checkpoint_ref = _publish_fixed_generation_checkpoint(
                    operation_id, lambda: os.read(source_fd, 1024 * 1024),
                )
            finally:
                os.close(source_fd)
            after = base.lstat()
            if checkpoint_ref is None or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            ) or checkpoint_ref["size"] != before.st_size:
                return {"ok": False, "reason": "checkpoint_write_failed", "base_path": str(base), "backup_path": str(backup)}
            old_hash = checkpoint_ref["sha256"]
            # Policy classification uses the same complete preimage that the
            # legacy path examined; generated configs have no 4 MiB input cap.
            original = base.read_bytes()
            if hashlib.sha256(original).hexdigest() != old_hash:
                return {"ok": False, "reason": "checkpoint_source_changed", "base_path": str(base), "backup_path": str(backup)}
        else:
            original = base.read_bytes()
            old_hash = hashlib.sha256(original).hexdigest()
        if operation_id is None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.parent.chmod(0o700)
            atomic_write_text(backup, original.decode("utf-8"))
            backup.chmod(0o600)
            checkpoint_ref = {"path": str(backup), "sha256": hashlib.sha256(original).hexdigest(), "size": len(original)}
        else:
            assert checkpoint_ref is not None
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
        "checkpoint_ref": checkpoint_ref,
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
        if not isinstance(targets, list) or not targets or "DIRECT" in targets:
            return False
        fixed_global_default = False
        if name == "vpn-global" and len(targets) == 2 and targets[0] == "REJECT":
            from fwrouter_api.adapters.mihomo import NON_SERVER_PROXY_TARGETS

            fixed_global_default = str(targets[1]) not in NON_SERVER_PROXY_TARGETS
        if targets[-1] != "REJECT" and not fixed_global_default:
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
    generation_materialization_callback: Any = None,
    generation_reservation_callback: Any = None,
    generation_completion_callback: Any = None,
    fixed_generation_reservation: dict[str, Any] | None = None,
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

    resume_reservation = fixed_generation_reservation if isinstance(fixed_generation_reservation, dict) else None
    operation_id = str((resume_reservation or {}).get("operation_id") or uuid4())
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
    reservation_source_revision = expected_selection_revision
    if resume_reservation is not None:
        if (
            not callable(generation_reservation_callback)
            or type(resume_reservation.get("before_revision")) is not int
            or type(resume_reservation.get("selection_revision")) is not int
            or resume_reservation.get("selection_revision") != expected_selection_revision
            or not isinstance(resume_reservation.get("operation_id"), str)
            or not operation_id
        ):
            return {
                "ok": False, "stage": "generation_resume_reservation",
                "error_code": "MIHOMO_GENERATION_RESERVATION_INVALID",
                "last_good_retained": False,
                "checkpoint_retained": _fixed_generation_checkpoint_matches(
                    operation_id, resume_reservation.get("checkpoint_ref"),
                ),
            }
        reservation_source_revision = int(resume_reservation["before_revision"])
        if expected_selection_revision != reservation_source_revision + 1:
            return {
                "ok": False, "stage": "generation_resume_reservation",
                "error_code": "MIHOMO_GENERATION_RESERVATION_INVALID",
                "last_good_retained": False,
                "checkpoint_retained": _fixed_generation_checkpoint_matches(
                    operation_id, resume_reservation.get("checkpoint_ref"),
                ),
            }
    runtime_incarnation_before = _mihomo_incarnation()
    if not runtime_incarnation_before:
        return {
            "ok": False, "stage": "generation_snapshot",
            "error_code": "MIHOMO_RUNTIME_IDENTITY_UNAVAILABLE",
            "error_message": "Mihomo runtime incarnation is unavailable; refusing an unfenced generation mutation.",
            "last_good_retained": resume_reservation is None,
            "checkpoint_retained": bool(
                isinstance(resume_reservation, dict)
                and _fixed_generation_checkpoint_matches(
                    operation_id, resume_reservation.get("checkpoint_ref"),
                )
            ),
            "operation_id": operation_id,
        }
    base_path = config._resolved_base_config_path()
    active_config_hash_before = _file_hash(base_path)

    def last_good_still_active() -> bool:
        if resume_reservation is None:
            return True
        return bool(
            active_config_hash_before == resume_reservation.get("before_active_config_hash")
            and runtime_incarnation_before == resume_reservation.get("before_runtime_incarnation")
        )

    def fixed_checkpoint_retained() -> bool:
        return bool(
            isinstance(resume_reservation, dict)
            and _fixed_generation_checkpoint_matches(
                operation_id, resume_reservation.get("checkpoint_ref"),
            )
        )

    input_fingerprint: dict[str, Any] | None = None
    try:
        input_fingerprint = current_mihomo_input_fingerprint(routing_dict)
    except Exception:
        input_fingerprint = None
    if resume_reservation is not None and (
        not isinstance(input_fingerprint, dict)
        or input_fingerprint.get("hash") != resume_reservation.get("input_fingerprint_hash")
    ):
        return {
            "ok": False, "stage": "generation_resume_reservation",
            "error_code": "MIHOMO_GENERATION_RESERVATION_STALE",
            "last_good_retained": False,
            "checkpoint_retained": _fixed_generation_checkpoint_matches(
                operation_id, resume_reservation.get("checkpoint_ref"),
            ),
            "operation_id": operation_id,
        }
    if resume_reservation is not None:
        try:
            with xray_writer_guard(timeout_seconds=30.0):
                reservation_result = generation_reservation_callback(
                    mode="validate",
                    operation_id=operation_id,
                    expected_selection_revision=expected_selection_revision,
                    reservation=resume_reservation,
                    candidate_config_hash=resume_reservation.get("candidate_config_hash"),
                    input_fingerprint_hash=(input_fingerprint or {}).get("hash"),
                    active_config_hash=active_config_hash_before,
                    runtime_incarnation=runtime_incarnation_before,
                ) if callable(generation_reservation_callback) else None
        except Exception:
            reservation_result = None
        if (
            not isinstance(reservation_result, dict)
            or reservation_result.get("ok") is not True
            or reservation_result.get("selection_revision") != expected_selection_revision
        ):
            return {
                "ok": False, "stage": "generation_resume_reservation",
                "error_code": "MIHOMO_GENERATION_RESERVATION_STALE",
                "last_good_retained": last_good_still_active(),
                "checkpoint_retained": fixed_checkpoint_retained(),
                "operation_id": operation_id,
            }
    fixed_generation_reserved = resume_reservation is not None
    fixed_receipt_publication_failed = False

    def publish_fixed_generation_receipt(
        *, active_hash: str, runtime_incarnation: str, native_proof: dict[str, Any],
        unchanged_config: bool,
    ) -> dict[str, Any] | None:
        if not fixed_generation_reserved or not callable(generation_completion_callback):
            return None
        receipt = {
            "schema": "mihomo-fixed-generation-receipt/v1",
            "operation_id": operation_id,
            "job_id": job_id,
            "before_revision": reservation_source_revision,
            "selection_revision": expected_selection_revision,
            "input_fingerprint_hash": (input_fingerprint or {}).get("hash"),
            "candidate_config_hash": _file_hash(config._resolved_candidate_config_path()),
            "active_config_hash": active_hash,
            "runtime_incarnation": runtime_incarnation,
            "native_udp_policy_verified": native_proof.get("ok") is True,
            "native_top_level_rules_match": native_proof.get("native_top_level_rules_match") is True,
            "native_selector_membership_matches": native_proof.get("selector_membership_matches") is True,
            "guarded_rules_match": native_proof.get("guarded_rules_match") is True,
            "publication_verified": True,
            "unchanged_config": bool(unchanged_config),
        }
        try:
            completion = generation_completion_callback(
                operation_id=operation_id,
                expected_selection_revision=expected_selection_revision,
                receipt=receipt,
            ) if callable(generation_completion_callback) else None
        except Exception:
            completion = None
        if not isinstance(completion, dict) or completion.get("ok") is not True:
            return None
        return receipt

    if (
        resume_reservation is None
        and input_fingerprint is not None
        and mihomo_input_unchanged(input_fingerprint)
    ):
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
            if (
                type(owned_revision) is not int
                or owned_revision != expected_selection_revision
                or not _selection_publication_still_owned(
                operation_id=operation_id, expected_revision=owned_revision,
                expected_incarnation=runtime_incarnation_before,
                expected_input_hash=(input_fingerprint or {}).get("hash"),
                expected_active_hash=active_config_hash_before,
                routing=routing_dict, verification=verification,
                )
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
            if not _selection_publication_still_owned(
                operation_id=operation_id, expected_revision=expected_selection_revision,
                expected_incarnation=runtime_incarnation_before,
                expected_input_hash=(input_fingerprint or {}).get("hash"),
                expected_active_hash=active_config_hash_before,
                routing=routing_dict, verification=verification,
            ):
                return {
                    "ok": False,
                    "stage": "generation_publication_revalidation",
                    "error_code": "MIHOMO_GENERATION_STALE_AFTER_RECEIPT",
                    "reconcile_reason": "selection_or_runtime_superseded",
                    "last_good_retained": True,
                }
            fixed_receipt = None
            if fixed_generation_reserved:
                fixed_receipt = publish_fixed_generation_receipt(
                    active_hash=active_config_hash_before,
                    runtime_incarnation=runtime_incarnation_before,
                    native_proof=native_proof,
                    unchanged_config=False,
                )
                if fixed_receipt is None:
                    return {
                        "ok": False, "stage": "generation_receipt_publication",
                        "error_code": "MIHOMO_GENERATION_RECEIPT_PUBLICATION_FAILED",
                        "last_good_retained": True, "operation_id": operation_id,
                    }
        base_path = config._resolved_base_config_path()
        candidate_path = config._resolved_candidate_config_path()
        return {
            "ok": True,
            "operation_id": operation_id,
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
            "reentry_generation_fence": ({
                "schema": "mihomo-reentry-generation-fence/v1",
                "operation_id": operation_id,
                "job_id": job_id,
                "before_revision": reservation_source_revision,
                "selection_revision": expected_selection_revision,
                "runtime_incarnation": runtime_incarnation_before,
                "active_config_hash": active_config_hash_before,
                "native_proof_verified": native_proof.get("ok") is True,
                "publication_verified": True,
                "unchanged_config": not fixed_generation_reserved,
            } if verification is not None else None),
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
    if resume_reservation is not None:
        checkpoint_ref = resume_reservation.get("checkpoint_ref")
        current_active_hash = _file_hash(base_path)
        if (
            not _fixed_generation_checkpoint_matches(operation_id, checkpoint_ref)
            or prepared_candidate_hash != resume_reservation.get("candidate_config_hash")
            or (input_fingerprint or {}).get("hash") != resume_reservation.get("input_fingerprint_hash")
            or current_active_hash not in {
                resume_reservation.get("before_active_config_hash"),
                resume_reservation.get("candidate_config_hash"),
            }
        ):
            return {
                "ok": False, "stage": "generation_resume_reservation",
                "error_code": "MIHOMO_GENERATION_RESERVATION_STALE",
                "last_good_retained": last_good_still_active(),
                "checkpoint_retained": fixed_checkpoint_retained(),
                "operation_id": operation_id,
            }
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

    if (
        resume_reservation is not None
        and _file_hash(base_path) == resume_reservation.get("candidate_config_hash")
        and runtime_incarnation_before == resume_reservation.get("before_runtime_incarnation")
    ):
        # Promotion may have completed before a crash while the native restart
        # did not. Continue the same operation through its restart owner instead
        # of treating file equality as proof that the runtime loaded it.
        files_match = False

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
            "operation_id": operation_id,
            "verification_callback_result": verification,
            "last_good_retained": last_good_still_active(),
            "checkpoint_retained": fixed_checkpoint_retained(),
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
                        elif not _selection_publication_still_owned(
                            operation_id=operation_id,
                            expected_revision=owned_revision,
                            expected_incarnation=runtime_incarnation_before,
                            expected_input_hash=(input_fingerprint or {}).get("hash"),
                            expected_active_hash=active_config_hash_before,
                            routing=routing_dict,
                            verification=verification,
                        ):
                            result.update({
                                "ok": False,
                                "stage": "generation_publication_revalidation",
                                "error_code": "MIHOMO_GENERATION_STALE_AFTER_RECEIPT",
                                "reconcile_reason": "selection_or_runtime_superseded",
                                "last_good_retained": True,
                            })
                        elif verification is not None and result.get("ok"):
                            fixed_receipt = None
                            if fixed_generation_reserved:
                                fixed_receipt = publish_fixed_generation_receipt(
                                    active_hash=active_config_hash_before,
                                    runtime_incarnation=runtime_incarnation_before,
                                    native_proof=native_proof,
                                    unchanged_config=False,
                                )
                                if fixed_receipt is None:
                                    result.update({
                                        "ok": False,
                                        "stage": "generation_receipt_publication",
                                        "error_code": "MIHOMO_GENERATION_RECEIPT_PUBLICATION_FAILED",
                                        "last_good_retained": True,
                                    })
                            if result.get("ok"):
                                result["reentry_generation_fence"] = {
                                    "schema": "mihomo-reentry-generation-fence/v1",
                                    "operation_id": operation_id,
                                    "job_id": job_id,
                                    "before_revision": reservation_source_revision,
                                    "selection_revision": owned_revision,
                                    "runtime_incarnation": runtime_incarnation_before,
                                    "active_config_hash": active_config_hash_before,
                                    "native_proof_verified": native_proof.get("ok") is True,
                                    "publication_verified": True,
                                    "unchanged_config": not fixed_generation_reserved,
                                }
                    elif verification is not None:
                        result.update({"ok": False, "stage": "native_udp_policy_receipt",
                                       "reconcile_reason": "input_fingerprint_unavailable",
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
        if resume_reservation is not None:
            checkpoint_ref = resume_reservation.get("checkpoint_ref")
            if (
                not _fixed_generation_checkpoint_matches(operation_id, checkpoint_ref)
                or prepared_candidate_hash != resume_reservation.get("candidate_config_hash")
                or (input_fingerprint or {}).get("hash") != resume_reservation.get("input_fingerprint_hash")
            ):
                return {
                    "ok": False, "stage": "generation_resume_reservation",
                    "error_code": "MIHOMO_GENERATION_RESERVATION_STALE",
                    "last_good_retained": True, "operation_id": operation_id,
                }
            current_active_hash = _file_hash(base_path)
            if current_active_hash not in {
                resume_reservation.get("before_active_config_hash"), prepared_candidate_hash,
            }:
                return {
                    "ok": False, "stage": "generation_resume_reservation",
                    "error_code": "MIHOMO_GENERATION_RESERVATION_STALE",
                    "last_good_retained": True, "operation_id": operation_id,
                }
            if (
                current_active_hash == resume_reservation.get("before_active_config_hash")
                and current_incarnation != resume_reservation.get("before_runtime_incarnation")
            ):
                return {
                    "ok": False, "stage": "generation_resume_reservation",
                    "error_code": "MIHOMO_GENERATION_RESERVATION_STALE",
                    "last_good_retained": True, "operation_id": operation_id,
                }
            recovery_checkpoint = {
                "ok": True,
                "base_path": base_path,
                "backup_path": checkpoint_ref["path"],
                "before_hash": resume_reservation.get("before_active_config_hash"),
                "candidate_hash": prepared_candidate_hash,
                "checkpoint_ref": checkpoint_ref,
                "operation_id": operation_id,
                "input_fingerprint_hash": input_fingerprint.get("hash"),
                "runtime_incarnation_before": resume_reservation.get("before_runtime_incarnation"),
            }
        else:
            recovery_checkpoint = (
                _capture_mihomo_reconcile_checkpoint(
                    base_path, candidate_path, routing=routing_dict,
                    operation_id=operation_id if callable(generation_reservation_callback) else None,
                )
                if callable(verification_callback)
                else {"ok": False, "reason": "verification_callback_not_requested"}
            )
        if callable(generation_reservation_callback) and not recovery_checkpoint.get("ok"):
            return {
                "ok": False, "stage": "generation_checkpoint",
                "error_code": "MIHOMO_GENERATION_CHECKPOINT_UNAVAILABLE",
                "last_good_retained": True, "operation_id": operation_id,
            }
        with db_session() as connection:
            current_revision = read_selection_revision(connection)
            if (
                current_revision != expected_selection_revision
                or (input_fingerprint or {}).get("hash") != (current_fingerprint or {}).get("hash")
                or not prepared_candidate_hash
                or _file_hash(candidate_path) != prepared_candidate_hash
                or _file_hash(base_path) != active_config_hash_before
                or (
                    resume_reservation is None
                    and current_incarnation != runtime_incarnation_before
                )
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
            if callable(generation_reservation_callback):
                generation_revision = None
            else:
                generation_revision = advance_selection_revision(
                    connection, expected_revision=current_revision
                )
        if callable(generation_reservation_callback):
            if resume_reservation is not None:
                reservation_result = generation_reservation_callback(
                    mode="validate",
                    operation_id=operation_id,
                    expected_selection_revision=current_revision,
                    reservation=resume_reservation,
                    candidate_config_hash=prepared_candidate_hash,
                    input_fingerprint_hash=(input_fingerprint or {}).get("hash"),
                    active_config_hash=_file_hash(base_path),
                    runtime_incarnation=current_incarnation,
                )
            else:
                reservation = {
                    "schema": "mihomo-fixed-generation-reservation/v1",
                    "operation_id": operation_id,
                    "job_id": job_id,
                    "before_revision": current_revision,
                    "selection_revision": current_revision + 1,
                    "input_fingerprint_hash": (input_fingerprint or {}).get("hash"),
                    "candidate_config_hash": prepared_candidate_hash,
                    "before_active_config_hash": active_config_hash_before,
                    "before_runtime_incarnation": runtime_incarnation_before,
                    "checkpoint_ref": recovery_checkpoint.get("checkpoint_ref"),
                }
                reservation_result = generation_reservation_callback(
                    mode="reserve", reservation=reservation,
                    expected_selection_revision=current_revision,
                )
            if (
                not isinstance(reservation_result, dict)
                or reservation_result.get("ok") is not True
                or type(reservation_result.get("selection_revision")) is not int
            ):
                return {
                    "ok": False, "stage": "generation_reservation",
                    "error_code": "MIHOMO_GENERATION_RESERVATION_FAILED",
                    "last_good_retained": True, "operation_id": operation_id,
                }
            generation_revision = int(reservation_result["selection_revision"])
            fixed_generation_reserved = True
            reservation_source_revision = int(reservation_result.get("before_revision", reservation_source_revision))
            if resume_reservation is None:
                resume_reservation = reservation
            expected_selection_revision = generation_revision

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
        restart_kwargs: dict[str, Any] = {
            "action": restart_action,
            "selection_fenced": True,
            "expected_selection_revision": generation_revision,
        }
        fixed_server_id = str((routing_dict or {}).get("desired_fixed_server_id") or "").strip()
        if (
            str((routing_dict or {}).get("server_mode") or "auto").strip().lower() == "fixed"
            and fixed_server_id
            and callable(generation_materialization_callback)
        ):
            restart_kwargs.update({
                "generation_verification_callback": generation_materialization_callback,
                "operation_id": operation_id,
            })
        restarted = config.restart_mihomo_container(**restart_kwargs)
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
    fixed_receipt: dict[str, Any] | None = None
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
            if publication_owned and input_fingerprint is not None:
                publication_owned = _selection_publication_still_owned(
                    operation_id=operation_id,
                    expected_revision=recovery_expected_revision,
                    expected_incarnation=runtime_incarnation_after,
                    expected_input_hash=(input_fingerprint or {}).get("hash"),
                    expected_active_hash=prepared_candidate_hash,
                    routing=routing_dict,
                    verification=verification,
                )
            if publication_owned and fixed_generation_reserved:
                fixed_receipt = publish_fixed_generation_receipt(
                    active_hash=prepared_candidate_hash,
                    runtime_incarnation=runtime_incarnation_after,
                    native_proof=native_proof,
                    unchanged_config=False,
                )
                fixed_receipt_publication_failed = fixed_receipt is None
                publication_owned = fixed_receipt is not None
        if not publication_owned:
            publication_revalidation_failed = True
            verified = False
    if not verified and promoted.get("promoted"):
        if fixed_generation_reserved:
            # A fixed workflow is durable across process restart. Do not restore
            # a prior snapshot or overwrite a newer fixed/auto intent; leave the
            # operation reserved for exact owner-bound proof/retry.
            recovery = {
                "ok": False,
                "recovered": False,
                "checkpoint_retained": _fixed_generation_checkpoint_matches(
                    operation_id, recovery_checkpoint.get("checkpoint_ref"),
                ),
                "reason": "fixed_workflow_remains_reserved",
            }
        else:
            recovery = restore_mihomo_reconcile_checkpoint(
                recovery_checkpoint,
                expected_revision=recovery_expected_revision,
                operation_id=operation_id,
            )
    result = {
        "ok": verified,
        "operation_id": operation_id,
        "stage": ("generation_receipt_publication" if fixed_receipt_publication_failed else
                  ("generation_publication_revalidation" if publication_revalidation_failed else
                   ("verification" if verification is not None and not verification.get("ok") else None))),
        "error_code": ("MIHOMO_GENERATION_RECEIPT_PUBLICATION_FAILED" if fixed_receipt_publication_failed else
                       ("MIHOMO_GENERATION_STALE_BEFORE_PUBLICATION" if publication_revalidation_failed else None)),
        "verification_callback_result": verification,
        "last_good_retained": bool(recovery.get("recovered")) if recovery is not None else (not bool(promoted.get("promoted")) if not promoted.get("ok") else False),
        "checkpoint_retained": (
            bool(recovery.get("checkpoint_retained"))
            if recovery is not None and fixed_generation_reserved
            else fixed_checkpoint_retained()
        ),
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
            "before_revision": reservation_source_revision,
            "selection_revision": recovery_expected_revision,
            "runtime_incarnation": runtime_incarnation_after,
            "active_config_hash": prepared_candidate_hash,
            "native_proof_verified": native_proof.get("ok") is True,
            "publication_verified": True,
            "unchanged_config": False,
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
