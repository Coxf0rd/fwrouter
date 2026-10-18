"""Durable revision fence for the existing Core/apply owner."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
from pathlib import Path

from fwrouter_api.db.connection import db_session


CORE_INTENT_REVISION_KEY = "core.intent_revision"


def read_core_runtime_context() -> dict[str, str] | None:
    """Identify the boot and network namespace used by local nft executors."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().lower()
        namespace_inode = os.stat("/proc/self/ns/net", follow_symlinks=True).st_ino
    except (OSError, UnicodeError):
        return None
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", boot_id):
        return None
    if type(namespace_inode) is not int or namespace_inode <= 0:
        return None
    return {"boot_id": boot_id, "network_namespace_inode": str(namespace_inode)}


def read_core_intent_revision(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value_json FROM settings WHERE key=?", (CORE_INTENT_REVISION_KEY,),
    ).fetchone()
    if row is None:
        raise ValueError("CORE_INTENT_REVISION_MISSING")
    try:
        value = json.loads(str(row["value_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("CORE_INTENT_REVISION_INVALID") from exc
    if type(value) is not int or value < 0:
        raise ValueError("CORE_INTENT_REVISION_INVALID")
    return value


def advance_core_intent_revision(connection: sqlite3.Connection) -> int:
    current = read_core_intent_revision(connection)
    next_revision = current + 1
    cursor = connection.execute(
        "UPDATE settings SET value_json=?, updated_at=CURRENT_TIMESTAMP "
        "WHERE key=? AND value_json=?",
        (json.dumps(next_revision), CORE_INTENT_REVISION_KEY, json.dumps(current)),
    )
    if cursor.rowcount != 1:
        raise ValueError("CORE_INTENT_REVISION_CONFLICT")
    return next_revision


def current_core_intent_revision() -> int:
    with db_session() as connection:
        return read_core_intent_revision(connection)


def assert_core_intent_current(
    connection: sqlite3.Connection, expected_revision: int, *, require_pending: bool = True,
) -> bool:
    if read_core_intent_revision(connection) != expected_revision:
        return False
    module = connection.execute(
        "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
    ).fetchone()
    bypass_row = connection.execute(
        "SELECT value_json FROM settings WHERE key='core.bypass'",
    ).fetchone()
    if module is None or tuple(module)[:2] != ("enabled", "managed"):
        return False
    if require_pending and module["apply_state"] != "pending":
        return False
    if bypass_row is not None:
        try:
            bypass = json.loads(str(bypass_row["value_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(bypass, dict) or type(bypass.get("enabled", False)) is not bool:
            return False
        if bypass.get("enabled"):
            return False
    return True


def assert_core_intent_snapshot_current(
    connection: sqlite3.Connection, expected_revision: int,
    expected_module_snapshot: tuple[str, str, str] | None,
) -> bool:
    if read_core_intent_revision(connection) != expected_revision:
        return False
    module = connection.execute(
        "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
    ).fetchone()
    return module is not None and expected_module_snapshot is not None and tuple(module) == expected_module_snapshot


def settle_core_enable_after_proof(
    *, expected_revision: int, apply_id: str, proof: dict[str, object],
    expected_runtime_context: dict[str, str],
) -> bool:
    """CAS Core pending to clean only for the latest owned apply and fresh proof."""
    if (
        proof.get("ok") is not True or proof.get("apply_id") != apply_id
        or proof.get("traffic_enforcement_guaranteed") is not True
        or proof.get("active_mode_matches_intent") is not True
        or not isinstance(proof.get("manifest_hash"), str)
        or proof.get("runtime_context") != expected_runtime_context
    ):
        return False
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if read_core_runtime_context() != expected_runtime_context:
            return False
        if not assert_core_intent_current(connection, expected_revision):
            return False
        latest = connection.execute(
            "SELECT apply_id, status, core_intent_revision FROM apply_versions "
            "ORDER BY created_at DESC, apply_id DESC LIMIT 1",
        ).fetchone()
        routing = connection.execute(
            "SELECT desired_mode, applied_mode, selective_default, server_mode, "
            "desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
            "active_auto_server_id, apply_state FROM routing_global_state WHERE id=1",
        ).fetchone()
        if (
            latest is None or latest["apply_id"] != apply_id or latest["status"] != "applied"
            or latest["core_intent_revision"] != expected_revision
            or routing is None or routing["apply_state"] != "clean"
            or routing["desired_mode"] != routing["applied_mode"]
            or proof.get("live_global_mode") != routing["applied_mode"]
            or proof.get("enforcement_level") != {
                "direct": "global_direct_enforced",
                "selective": "global_selective_enforced",
                "vpn": "global_vpn_enforced",
            }.get(str(routing["applied_mode"] or ""))
        ):
            return False
        from fwrouter_api.services.dataplane_global import _read_applied_manifest_uncached
        manifest = _read_applied_manifest_uncached()
        if not isinstance(manifest, dict) or manifest.get("plan_id") != apply_id:
            return False
        current_manifest_hash = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"),
        ).hexdigest()
        if current_manifest_hash != proof.get("manifest_hash"):
            return False
        manifest_routing = manifest.get("routing_global_state")
        routing_keys = (
            "desired_mode", "applied_mode", "selective_default", "server_mode",
            "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until",
            "active_auto_server_id",
        )
        if not isinstance(manifest_routing, dict) or any(
            str(manifest_routing.get(key) or "") != str(routing[key] or "")
            for key in routing_keys
        ):
            return False
        changed = connection.execute(
            "UPDATE modules SET apply_state='clean', error_code=NULL, error_message=NULL, "
            "status_text='Core apply verified against current runtime.', updated_at=CURRENT_TIMESTAMP "
            "WHERE module_name='core' AND desired_state='enabled' AND lifecycle_mode='managed' "
            "AND apply_state='pending'",
        ).rowcount
        return changed == 1
