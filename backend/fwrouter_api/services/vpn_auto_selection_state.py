"""Persistent fencing primitives for the Core-owned vpn-auto selection."""

from __future__ import annotations

import json
from typing import Any


SELECTION_REVISION_KEY = "routing.auto_selection_revision"
SELECTION_PROVENANCE_KEY = "routing.auto_selection_provenance"


def _read_json_int(value: Any) -> int:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("VPN-auto selection revision is malformed.") from exc
    if type(parsed) is not int:
        raise ValueError("VPN-auto selection revision must be a JSON integer.")
    revision = parsed
    if revision < 0:
        raise ValueError("VPN-auto selection revision cannot be negative.")
    return revision


def read_selection_revision(connection: Any) -> int:
    row = connection.execute(
        "SELECT value_json FROM settings WHERE key = ?",
        (SELECTION_REVISION_KEY,),
    ).fetchone()
    return 0 if row is None else _read_json_int(row["value_json"])


def read_selection_fence(connection: Any) -> dict[str, Any]:
    row = connection.execute(
        "SELECT active_auto_server_id FROM routing_global_state WHERE id = 1"
    ).fetchone()
    provenance_row = connection.execute(
        "SELECT value_json FROM settings WHERE key = ?",
        (SELECTION_PROVENANCE_KEY,),
    ).fetchone()
    if provenance_row is None:
        provenance: dict[str, Any] = {}
    else:
        try:
            value = json.loads(provenance_row["value_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("VPN-auto selection provenance is malformed.") from exc
        if not isinstance(value, dict):
            raise ValueError("VPN-auto selection provenance must be a JSON object.")
        provenance = value
    return {
        "revision": read_selection_revision(connection),
        "active_server_id": str(row["active_auto_server_id"] or "").strip() or None if row else None,
        "decision_id": str(provenance.get("decision_id") or "").strip() or None,
        "provenance_server_id": str(provenance.get("selected_server_id") or "").strip() or None,
        "routing_state_present": row is not None,
    }


def _selection_relevant_server_raw(raw_json: Any) -> str:
    """Canonicalize Mihomo proxy snapshots without volatile probe telemetry."""
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except (TypeError, ValueError, json.JSONDecodeError):
        return str(raw_json or "")
    if not isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    volatile_keys = {"history", "alive", "delay", "last-test", "last_test", "lasttest"}
    durable = {key: value for key, value in raw.items() if str(key).strip().lower() not in volatile_keys}
    return json.dumps(durable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def selection_pool_signature(connection: Any) -> str:
    """Hash only durable inputs that can change Core's eligible execution pool."""
    from fwrouter_api.services.auto_eligibility import auto_eligible_sql

    rows = connection.execute(
        f"""SELECT s.server_id, s.raw_json, COALESCE(p.vpn_auto_priority, 0) AS priority,
                   COALESCE(pb.provider_id, '') AS provider_kind,
                   COALESCE(pb.resource_id, '') AS resource_id,
                   COALESCE(pb.protocol, '') AS provider_protocol,
                   COALESCE(pb.enabled, 0) AS binding_enabled
            FROM servers s
            LEFT JOIN server_preferences p ON p.server_id=s.server_id
            LEFT JOIN provider_bindings pb ON pb.logical_server_id=s.server_id AND pb.enabled=1
            WHERE {auto_eligible_sql()}
            ORDER BY s.server_id, pb.source_ref"""
    ).fetchall()
    import hashlib
    material = [
        (str(row["server_id"]), hashlib.sha256(_selection_relevant_server_raw(row["raw_json"]).encode("utf-8")).hexdigest(),
         *(row[key] for key in row.keys() if key not in {"server_id", "raw_json"}))
        for row in rows
    ]
    exclusive = connection.execute(
        "SELECT value_json FROM settings WHERE key='vpn_auto_exclusive_source_ref'"
    ).fetchone()
    material.append(("exclusive", str(exclusive["value_json"]) if exclusive else ""))
    routing_policy = connection.execute(
        "SELECT server_mode FROM routing_global_state WHERE id=1"
    ).fetchone()
    material.append(("server_mode", str(routing_policy["server_mode"] or "auto") if routing_policy else "auto"))
    members = connection.execute(
        """SELECT pm.source_ref, pm.provider_member_id, pm.location_id, pm.protocol,
                  pm.provider_status, pm.provider_status_source, pm.auto_enabled,
                  pm.priority, pm.advertised
           FROM provider_members pm
           JOIN provider_bindings pb ON pb.source_ref=pm.source_ref AND pb.enabled=1
           ORDER BY pm.source_ref, pm.provider_member_id, pm.location_id, pm.protocol"""
    ).fetchall()
    material.append(("provider_members", [tuple(row) for row in members]))
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def update_imported_routing_intent(connection: Any, routing: dict[str, Any], *, preserved_active_server_id: str | None) -> int | None:
    """Import non-selection routing intent while retaining Core-owned Auto state."""
    row = connection.execute("SELECT id FROM routing_global_state WHERE id=1").fetchone()
    if row is None:
        connection.execute(
            """INSERT INTO routing_global_state (id, desired_mode, applied_mode, selective_default,
               server_mode, desired_fixed_server_id, applied_fixed_server_id, fixed_server_until,
               active_auto_server_id, apply_state, error_code, error_message)
               VALUES (1, 'direct', 'direct', 'direct', 'auto', NULL, NULL, NULL, NULL, 'pending', NULL, NULL)"""
        )
    current_policy = connection.execute("SELECT server_mode FROM routing_global_state WHERE id=1").fetchone()
    current_active = connection.execute("SELECT active_auto_server_id FROM routing_global_state WHERE id=1").fetchone()
    if preserved_active_server_id:
        exists = connection.execute("SELECT 1 FROM servers WHERE server_id=?", (preserved_active_server_id,)).fetchone()
        if exists is None:
            return None
    changed_policy = str(current_policy["server_mode"] or "auto") != str(routing.get("server_mode") or "auto")
    connection.execute(
        """UPDATE routing_global_state SET desired_mode=?, applied_mode=?, selective_default=?,
           server_mode=?, desired_fixed_server_id=?, applied_fixed_server_id=?, fixed_server_until=?,
           active_auto_server_id=?, apply_state=?, error_code=?, error_message=?, updated_at=COALESCE(?, CURRENT_TIMESTAMP)
           WHERE id=1""",
        (routing.get("desired_mode"), routing.get("applied_mode"), routing.get("selective_default"),
         routing.get("server_mode"), routing.get("desired_fixed_server_id"), routing.get("applied_fixed_server_id"),
         routing.get("fixed_server_until"), preserved_active_server_id if preserved_active_server_id is not None else
         (current_active["active_auto_server_id"] if current_active else None), routing.get("apply_state"),
         routing.get("error_code"), routing.get("error_message"), routing.get("updated_at")),
    )
    return 1 if changed_policy else 0


def advance_selection_revision(
    connection: Any,
    *,
    expected_revision: int | None = None,
) -> int | None:
    """Advance under the caller's short write transaction; return None on CAS miss."""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    current = read_selection_revision(connection)
    if expected_revision is not None and current != int(expected_revision):
        return None
    next_revision = current + 1
    connection.execute(
        """INSERT INTO settings (key, value_json, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
             updated_at=CURRENT_TIMESTAMP""",
        (SELECTION_REVISION_KEY, json.dumps(next_revision)),
    )
    return next_revision


def commit_active_selection(
    connection: Any,
    *,
    expected_revision: int,
    expected_active_server_id: str | None,
    expected_provenance_decision_id: str | None,
    server_id: str,
    provenance: dict[str, Any],
) -> int | None:
    """CAS active ID and provenance with the next fence in one SQLite transaction."""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    current_revision = read_selection_revision(connection)
    if current_revision != int(expected_revision):
        return None
    row = connection.execute(
        "SELECT active_auto_server_id FROM routing_global_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    current_active = str(row["active_auto_server_id"] or "").strip()
    expected_active = str(expected_active_server_id or "").strip()
    if current_active != expected_active:
        return None
    provenance_row = connection.execute(
        "SELECT value_json FROM settings WHERE key = ?",
        (SELECTION_PROVENANCE_KEY,),
    ).fetchone()
    try:
        current_provenance = json.loads(provenance_row["value_json"]) if provenance_row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(current_provenance, dict):
        return None
    current_decision_id = str(current_provenance.get("decision_id") or "").strip() or None
    if current_decision_id != (str(expected_provenance_decision_id or "").strip() or None):
        return None
    next_revision = current_revision + 1
    updated = connection.execute(
        """UPDATE routing_global_state
           SET active_auto_server_id = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = 1""",
        (server_id,),
    )
    if updated.rowcount != 1:
        return None
    connection.execute(
        """INSERT INTO settings (key, value_json, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
             updated_at=CURRENT_TIMESTAMP""",
        (SELECTION_PROVENANCE_KEY, json.dumps(provenance, ensure_ascii=False, separators=(",", ":"))),
    )
    connection.execute(
        """INSERT INTO settings (key, value_json, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
             updated_at=CURRENT_TIMESTAMP""",
        (SELECTION_REVISION_KEY, json.dumps(next_revision)),
    )
    return next_revision
