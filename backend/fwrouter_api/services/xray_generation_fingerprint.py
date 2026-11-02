"""Canonical read projections used by Xray generation and admission fences.

Callers own SQLite connection lifetime and transaction boundaries. This module
only reads the supplied connection and returns stable projections/digests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any


def generation_source_projection(connection: sqlite3.Connection) -> list[Any]:
    return [
        [dict(row) for row in connection.execute("SELECT * FROM subscription_node_identities ORDER BY identity_id")],
        [dict(row) for row in connection.execute("SELECT * FROM subscription_node_aliases ORDER BY server_id")],
        [dict(row) for row in connection.execute("SELECT * FROM subscription_node_credentials ORDER BY scope, identity_id")],
        [dict(row) for row in connection.execute("SELECT * FROM subscription_node_credential_history ORDER BY client_uuid")],
        [dict(row) for row in connection.execute(
            "SELECT * FROM subscription_node_identity_transitions ORDER BY identity_id, from_revision"
        )],
        [dict(row) for row in connection.execute("SELECT source_id, server_id, is_active FROM subscription_server_memberships ORDER BY source_id, server_id")],
        [dict(row) for row in connection.execute("SELECT account_id, slug, enabled FROM subscription_accounts ORDER BY account_id").fetchall()],
        [dict(row) for row in connection.execute("SELECT client_id, account_id, token, enabled FROM subscription_clients ORDER BY client_id").fetchall()],
        [dict(row) for row in connection.execute("SELECT server_id, vpn_auto, vpn_auto_priority, manually_deleted_at FROM server_preferences ORDER BY server_id").fetchall()],
        [dict(row) for row in connection.execute("SELECT server_id, inventory_state, raw_json FROM servers ORDER BY server_id").fetchall()],
        [dict(row) for row in connection.execute("""SELECT subject_id, subject_type, subject_role, desired_mode, is_active, is_deleted,
            json_extract(metadata_json, '$.detail.client_id') AS client_id,
            json_extract(metadata_json, '$.detail.client_uuid') AS client_uuid,
            json_extract(metadata_json, '$.detail.email') AS email
            FROM subjects WHERE implementation_kind = 'xray' ORDER BY subject_id""").fetchall()],
        [dict(row) for row in connection.execute("SELECT subject_id, selected_server_id, selected_target_kind, selected_until FROM subject_server_overrides ORDER BY subject_id").fetchall()],
        [dict(row) for row in connection.execute("SELECT subject_id, override_mode, override_until FROM subject_user_overrides ORDER BY subject_id").fetchall()],
        [dict(row) for row in connection.execute("SELECT desired_mode, selective_default, server_mode FROM routing_global_state ORDER BY id").fetchall()],
        [dict(row) for row in connection.execute("SELECT key, value_json FROM settings WHERE key='vpn_auto_exclusive_source_ref'").fetchall()],
        [dict(row) for row in connection.execute("SELECT source_ref, enabled, logical_server_id, provider_id, resource_id, protocol, binding_revision FROM provider_bindings ORDER BY source_ref").fetchall()],
        [dict(row) for row in connection.execute("SELECT source_ref, provider_member_id, location_id, protocol, provider_status, auto_enabled, priority, advertised FROM provider_members ORDER BY source_ref, provider_member_id, location_id, protocol").fetchall()],
    ]


def source_projection_sha256(projection: list[Any]) -> str:
    """Hash one established generation/admission projection canonically."""
    return hashlib.sha256(json.dumps(projection, sort_keys=True, default=str).encode()).hexdigest()


def generation_source_fingerprint(connection: sqlite3.Connection) -> str:
    return source_projection_sha256(generation_source_projection(connection))


def legacy_admission_source_projection(
    connection: sqlite3.Connection,
    *,
    generation_fingerprint: Callable[[sqlite3.Connection], str],
) -> list[Any]:
    """Extend the historical source fence without changing its query contract."""
    values: list[Any] = [
        generation_fingerprint(connection),
        [dict(row) for row in connection.execute(
            "SELECT token, nodes_json, runtime_verified_at, updated_at "
            "FROM subscription_profile_snapshots ORDER BY token"
        ).fetchall()],
        [dict(row) for row in connection.execute(
            "SELECT id, desired_mode, applied_mode, selective_default, server_mode, "
            "desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
            "active_auto_server_id, apply_state, error_code "
            "FROM routing_global_state ORDER BY id"
        ).fetchall()],
        [dict(row) for row in connection.execute(
            "SELECT module_name, desired_state, lifecycle_mode FROM modules ORDER BY module_name"
        ).fetchall()],
        [dict(row) for row in connection.execute(
            "SELECT key, value_json FROM settings "
            "WHERE key IN ('routing.auto_selection_provenance', 'vpn_auto_exclusive_source_ref', 'core.bypass', 'core.intent_revision') "
            "ORDER BY key"
        ).fetchall()],
        [dict(row) for row in connection.execute(
            "SELECT apply_id, job_id, status, created_at, promoted_at, core_intent_revision "
            "FROM apply_versions ORDER BY apply_id"
        ).fetchall()],
        [dict(row) for row in connection.execute(
            "SELECT connection_id, system_id, label, connection_type, runtime_type, "
            "replacement_target, location, address, integration_mode, refresh_mode, enabled, value_json "
            "FROM external_connections ORDER BY connection_id"
        ).fetchall()],
    ]
    return values


def legacy_admission_source_fingerprint(
    connection: sqlite3.Connection,
    *,
    generation_fingerprint: Callable[[sqlite3.Connection], str],
) -> str:
    return source_projection_sha256(legacy_admission_source_projection(
        connection, generation_fingerprint=generation_fingerprint,
    ))


def legacy_admission_core_runtime_snapshot(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Capture Core/module apply observations using the caller's connection."""
    query = (
        "SELECT module_name, desired_state, lifecycle_mode, runtime_state, apply_state "
        "FROM modules WHERE module_name IN ('core', 'vpn', 'xray', 'watchdog', 'selector') "
        "ORDER BY module_name"
    )
    rows = [dict(row) for row in connection.execute(query).fetchall()]
    from fwrouter_api.services.core_intent import read_core_intent_revision

    try:
        revision = read_core_intent_revision(connection)
    except ValueError:
        revision = None
    return [*rows, {"core_intent_revision": revision}]


def legacy_admission_core_runtime_fingerprint(snapshot: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


def identity_projection_reference_sha256(connection: sqlite3.Connection) -> str:
    """Hash Xray subject/snapshot references used to fence intent compensation."""
    subjects = [
        [row["subject_id"], row["stable_key"], row["metadata_json"]]
        for row in connection.execute(
            "SELECT subject_id, stable_key, metadata_json FROM subjects "
            "WHERE implementation_kind='xray' ORDER BY subject_id"
        )
    ]
    for _, _, metadata_json in subjects:
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("IDENTITY_SUBJECT_PROJECTION_UNREADABLE") from exc
        if not isinstance(metadata, dict):
            raise ValueError("IDENTITY_SUBJECT_PROJECTION_UNREADABLE")
    snapshots = [
        [row["token"], row["nodes_json"], row["runtime_verified_at"], row["updated_at"]]
        for row in connection.execute(
            "SELECT token, nodes_json, runtime_verified_at, updated_at "
            "FROM subscription_profile_snapshots ORDER BY token"
        )
    ]
    for _, nodes_json, _, _ in snapshots:
        try:
            nodes = json.loads(nodes_json or "[]")
        except (TypeError, ValueError) as exc:
            raise ValueError("IDENTITY_SNAPSHOT_PROJECTION_UNREADABLE") from exc
        if not isinstance(nodes, list):
            raise ValueError("IDENTITY_SNAPSHOT_PROJECTION_UNREADABLE")
        if any(not isinstance(node, dict) for node in nodes):
            raise ValueError("IDENTITY_SNAPSHOT_PROJECTION_UNREADABLE")
    payload = json.dumps(
        {"subjects": subjects, "snapshots": snapshots},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
