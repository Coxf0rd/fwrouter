from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


PROVIDER_MANAGED_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_bindings (
    source_ref TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    resource_kind TEXT NOT NULL DEFAULT 'config',
    resource_id TEXT NOT NULL,
    logical_server_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    binding_revision INTEGER NOT NULL DEFAULT 1,
    current_member_id TEXT,
    current_location_id TEXT,
    observed_protocol TEXT,
    observed_at REAL,
    applied_member_id TEXT,
    applied_protocol TEXT,
    applied_at REAL,
    applied_revision INTEGER,
    last_outcome TEXT,
    available_configs_json TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_credentials (
    source_ref TEXT PRIMARY KEY REFERENCES provider_bindings(source_ref) ON DELETE CASCADE,
    api_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_locations (
    source_ref TEXT NOT NULL REFERENCES provider_bindings(source_ref) ON DELETE CASCADE,
    location_id TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (source_ref, location_id)
);
CREATE TABLE IF NOT EXISTS provider_members (
    source_ref TEXT NOT NULL REFERENCES provider_bindings(source_ref) ON DELETE CASCADE,
    provider_member_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    ip TEXT,
    available_slots INTEGER,
    provider_status TEXT NOT NULL DEFAULT 'unknown',
    provider_status_source TEXT NOT NULL DEFAULT 'absent',
    auto_enabled INTEGER NOT NULL DEFAULT 1 CHECK (auto_enabled IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority >= -1 AND priority <= 5),
    advertised INTEGER NOT NULL DEFAULT 1 CHECK (advertised IN (0, 1)),
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    last_seen_revision INTEGER NOT NULL,
    PRIMARY KEY (source_ref, provider_member_id, location_id, protocol)
);
CREATE INDEX IF NOT EXISTS idx_provider_members_scope
    ON provider_members(source_ref, location_id, protocol, advertised, last_seen_at);
CREATE TABLE IF NOT EXISTS provider_evidence (
    source_ref TEXT NOT NULL REFERENCES provider_bindings(source_ref) ON DELETE CASCADE,
    evidence_kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    observed_at REAL NOT NULL,
    safe_json TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'success',
    PRIMARY KEY (source_ref, evidence_kind, scope_key, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_provider_evidence_latest
    ON provider_evidence(source_ref, evidence_kind, scope_key, observed_at DESC);
"""

_CANDIDATE_EXCLUDED_PROVIDER_STATES = {"down", "unavailable", "busy"}


def provider_status_allows_candidate(status: object) -> bool:
    """Unknown provider state is neutral; explicit unavailable states reject a switch."""
    return str(status or "unknown").strip().lower() not in _CANDIDATE_EXCLUDED_PROVIDER_STATES


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create provider-owned tables on an existing DB connection; no external effects."""
    conn.executescript(PROVIDER_MANAGED_SCHEMA)
    conn.execute("DROP INDEX IF EXISTS idx_provider_bindings_one_enabled")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_bindings)")}
    if "available_configs_json" not in columns:
        conn.execute("ALTER TABLE provider_bindings ADD COLUMN available_configs_json TEXT NOT NULL DEFAULT '[]'")


def get_binding(conn: sqlite3.Connection, source_ref: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM provider_bindings WHERE source_ref = ?", (source_ref,)).fetchone()
    return dict(row) if row is not None else None


def list_bindings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM provider_bindings ORDER BY source_ref"
    ).fetchall()]


def save_binding(
    conn: sqlite3.Connection,
    source_ref: str,
    provider_id: str,
    resource_id: str | int,
    logical_server_id: str,
    protocol: str,
    enabled: bool,
    *,
    expected_revision: int | None = None,
    resource_kind: str = "config",
) -> dict[str, Any]:
    if not all((source_ref, provider_id, logical_server_id, protocol)):
        raise ValueError("provider binding fields are required")
    now = time.time()
    existing = get_binding(conn, source_ref)
    if existing is not None and expected_revision is not None and existing["binding_revision"] != expected_revision:
        raise RuntimeError("PROVIDER_BINDING_REVISION_CONFLICT")
    revision = int(existing["binding_revision"] if existing else 0) + 1
    conn.execute(
        """INSERT INTO provider_bindings
           (source_ref, provider_id, resource_kind, resource_id, logical_server_id, protocol,
            enabled, binding_revision, current_member_id, current_location_id, observed_protocol,
            observed_at, last_outcome, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_ref) DO UPDATE SET
             provider_id=excluded.provider_id, resource_kind=excluded.resource_kind,
             resource_id=excluded.resource_id, logical_server_id=excluded.logical_server_id,
             protocol=excluded.protocol, enabled=excluded.enabled,
             binding_revision=excluded.binding_revision,
             current_member_id=CASE WHEN provider_bindings.protocol=excluded.protocol
                                    THEN provider_bindings.current_member_id ELSE NULL END,
             current_location_id=CASE WHEN provider_bindings.protocol=excluded.protocol
                                      THEN provider_bindings.current_location_id ELSE NULL END,
             observed_protocol=CASE WHEN provider_bindings.protocol=excluded.protocol
                                    THEN provider_bindings.observed_protocol ELSE NULL END,
             observed_at=CASE WHEN provider_bindings.protocol=excluded.protocol
                              THEN provider_bindings.observed_at ELSE NULL END,
             last_outcome=excluded.last_outcome, updated_at=excluded.updated_at""",
        (source_ref, provider_id, resource_kind, str(resource_id), logical_server_id, protocol,
         int(enabled), revision, existing.get("current_member_id") if existing else None,
         existing.get("current_location_id") if existing else None,
         existing.get("observed_protocol") if existing else None,
         existing.get("observed_at") if existing else None,
         "pending" if enabled else "disabled", now),
    )
    result = get_binding(conn, source_ref)
    assert result is not None
    return result


def list_members(conn: sqlite3.Connection, source_ref: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM provider_members WHERE source_ref = ?
           ORDER BY location_id, protocol, provider_member_id""", (source_ref,)
    ).fetchall()
    return [dict(row) for row in rows]


def save_members(
    conn: sqlite3.Connection,
    source_ref: str,
    members: list[dict[str, Any]],
    *,
    revision: int,
    observed_at: float | None = None,
    expected_binding_revision: int | None = None,
) -> int:
    _check_revision(conn, source_ref, expected_binding_revision)
    now = observed_at if observed_at is not None else time.time()
    for member in members:
        member_id = member.get("provider_member_id", member.get("server_id"))
        location_id = member.get("location_id")
        protocol = member.get("protocol")
        slots = member.get("available_slots")
        ip = member.get("ip")
        provider_status = member.get("provider_status", "unknown")
        status_source = member.get("provider_status_source", "absent")
        if member_id is None or location_id is None or not protocol:
            raise ValueError("provider member identity is incomplete")
        if slots is not None and (not isinstance(slots, int) or slots < 0):
            raise ValueError("provider member slots are invalid")
        conn.execute(
            """INSERT INTO provider_members
                (source_ref, provider_member_id, location_id, protocol, ip, available_slots,
                provider_status, provider_status_source, advertised, first_seen_at, last_seen_at, last_seen_revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(source_ref, provider_member_id, location_id, protocol) DO UPDATE SET
                 ip=excluded.ip, available_slots=excluded.available_slots, advertised=1,
                 provider_status=excluded.provider_status, provider_status_source=excluded.provider_status_source,
                 last_seen_at=excluded.last_seen_at, last_seen_revision=excluded.last_seen_revision""",
            (source_ref, str(member_id), str(location_id), str(protocol), ip, slots,
             provider_status, status_source, now, now, revision),
        )
    return len(members)


def record_discovery(
    conn: sqlite3.Connection,
    source_ref: str,
    revision: int,
    location_id: str | int,
    protocol: str,
    members: list[dict[str, Any]],
    *,
    observed_at: float | None = None,
    expected_binding_revision: int | None = None,
) -> int:
    """Replace one advertised window while retaining omitted members as history."""
    _check_revision(conn, source_ref, expected_binding_revision)
    now = observed_at if observed_at is not None else time.time()
    conn.execute(
        "UPDATE provider_members SET advertised=0 WHERE source_ref=? AND location_id=? AND protocol=?",
        (source_ref, str(location_id), protocol),
    )
    normalized = [dict(member, location_id=location_id, protocol=protocol) for member in members]
    return save_members(conn, source_ref, normalized, revision=revision, observed_at=now,
                        expected_binding_revision=expected_binding_revision)


def update_member_preference(conn: sqlite3.Connection, source_ref: str, member_id: str | int,
                             location_id: str | int, protocol: str, *, auto_enabled: bool | None = None,
                             priority: int | None = None, expected_binding_revision: int) -> None:
    _check_revision(conn, source_ref, expected_binding_revision)
    if auto_enabled is None and priority is None:
        return
    if priority is not None and (priority < -1 or priority > 5):
        raise ValueError("provider member priority is out of range")
    assignments = []
    values: list[Any] = []
    if auto_enabled is not None:
        assignments.append("auto_enabled=?")
        values.append(int(auto_enabled))
    if priority is not None:
        assignments.append("priority=?")
        values.append(priority)
    values.extend((source_ref, str(member_id), str(location_id), protocol))
    cursor = conn.execute(
        f"""UPDATE provider_members SET {', '.join(assignments)} WHERE source_ref=?
           AND provider_member_id=? AND location_id=? AND protocol=?""",
        values,
    )
    if cursor.rowcount != 1:
        raise LookupError("provider member was not found")


def record_config(
    conn: sqlite3.Connection,
    source_ref: str,
    revision: int,
    safe_config: dict[str, Any],
    *,
    observed_at: float | None = None,
    outcome: str = "success",
    expected_binding_revision: int | None = None,
) -> None:
    """Persist allowlisted identity metadata only; connection_url/config material is discarded."""
    safe = {key: safe_config[key] for key in ("server_id", "location_id", "protocol") if key in safe_config}
    now = observed_at if observed_at is not None else time.time()
    _check_revision(conn, source_ref, expected_binding_revision)
    conn.execute(
        """INSERT INTO provider_evidence
           (source_ref, evidence_kind, scope_key, binding_revision, observed_at, safe_json, outcome)
           VALUES (?, 'config', 'current', ?, ?, ?, ?)""",
        (source_ref, revision, now, json.dumps(safe, separators=(",", ":")), outcome),
    )
    _trim_evidence(conn, source_ref, "config", "current", keep=32)
    if any(key in safe for key in ("server_id", "location_id", "protocol")):
        values: dict[str, Any] = {"observed_at": now, "last_outcome": outcome, "updated_at": now}
        if "server_id" in safe:
            values["current_member_id"] = str(safe["server_id"]) if safe["server_id"] is not None else None
        if "location_id" in safe:
            values["current_location_id"] = str(safe["location_id"]) if safe["location_id"] is not None else None
        if "protocol" in safe:
            values["observed_protocol"] = safe["protocol"]
        assignments = ", ".join(f"{column}=?" for column in values)
        conn.execute(f"UPDATE provider_bindings SET {assignments} WHERE source_ref=?",
                     (*values.values(), source_ref))
        if all(safe.get(key) is not None for key in ("server_id", "location_id", "protocol")):
            conn.execute(
                """INSERT INTO provider_members
                   (source_ref, provider_member_id, location_id, protocol, advertised,
                    first_seen_at, last_seen_at, last_seen_revision)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                   ON CONFLICT(source_ref, provider_member_id, location_id, protocol) DO NOTHING""",
                (source_ref, str(safe["server_id"]), str(safe["location_id"]), str(safe["protocol"]),
                 now, now, revision),
            )
    else:
        conn.execute("UPDATE provider_bindings SET last_outcome=?, updated_at=? WHERE source_ref=?",
                     (outcome, now, source_ref))


def update_observation(conn: sqlite3.Connection, source_ref: str, *, kind: str,
                       safe_data: dict[str, Any], revision: int,
                       observed_at: float | None = None, outcome: str = "success",
                       expected_binding_revision: int | None = None,
                       scope_key: str = "current") -> None:
    _check_revision(conn, source_ref, expected_binding_revision)
    allowed = {"status", "member_id", "protocol"}
    safe = {key: safe_data[key] for key in allowed if key in safe_data}
    now = observed_at if observed_at is not None else time.time()
    conn.execute(
        """INSERT INTO provider_evidence
           (source_ref, evidence_kind, scope_key, binding_revision, observed_at, safe_json, outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source_ref, kind, scope_key, revision, now, json.dumps(safe, separators=(",", ":")), outcome),
    )
    _trim_evidence(conn, source_ref, kind, scope_key, keep=32)


def record_applied(conn: sqlite3.Connection, source_ref: str, *, revision: int,
                   member_id: str | int, protocol: str,
                   applied_at: float | None = None,
                   expected_binding_revision: int | None = None) -> None:
    """Record a member only after the common apply/readback path verified it."""
    _check_revision(conn, source_ref, expected_binding_revision)
    now = applied_at if applied_at is not None else time.time()
    conn.execute(
        """UPDATE provider_bindings SET applied_member_id=?, applied_protocol=?, applied_at=?,
           applied_revision=?, updated_at=? WHERE source_ref=?""",
        (str(member_id), protocol, now, revision, now, source_ref),
    )


def latest_evidence(conn: sqlite3.Connection, source_ref: str, kind: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT evidence_kind, scope_key, binding_revision, observed_at, safe_json, outcome
           FROM provider_evidence WHERE source_ref=? AND evidence_kind=?
           ORDER BY observed_at DESC LIMIT 1""", (source_ref, kind)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["data"] = json.loads(result.pop("safe_json"))
    return result


def _check_revision(conn: sqlite3.Connection, source_ref: str,
                    expected_revision: int | None) -> None:
    if expected_revision is None:
        return
    binding = get_binding(conn, source_ref)
    if binding is None or int(binding["binding_revision"]) != expected_revision:
        raise RuntimeError("PROVIDER_BINDING_REVISION_CONFLICT")


def _trim_evidence(conn: sqlite3.Connection, source_ref: str, kind: str,
                   scope_key: str, *, keep: int) -> None:
    conn.execute(
        """DELETE FROM provider_evidence WHERE source_ref=? AND evidence_kind=? AND scope_key=?
           AND observed_at NOT IN (SELECT observed_at FROM provider_evidence
             WHERE source_ref=? AND evidence_kind=? AND scope_key=?
             ORDER BY observed_at DESC LIMIT ?)""",
        (source_ref, kind, scope_key, source_ref, kind, scope_key, keep),
    )


def credential_configured(conn: sqlite3.Connection, source_ref: str) -> bool:
    return conn.execute("SELECT 1 FROM provider_credentials WHERE source_ref=? AND api_key != ''", (source_ref,)).fetchone() is not None


def set_credential(conn: sqlite3.Connection, source_ref: str, api_key: str) -> None:
    """Backend-only secret in existing 0700/0600 operational SQLite storage."""
    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 8192:
        raise ValueError("PROVIDER_CREDENTIAL_INVALID")
    conn.execute("INSERT INTO provider_credentials(source_ref,api_key) VALUES (?,?) ON CONFLICT(source_ref) DO UPDATE SET api_key=excluded.api_key", (source_ref, api_key.strip()))


def get_credential(conn: sqlite3.Connection, source_ref: str) -> str | None:
    row = conn.execute("SELECT api_key FROM provider_credentials WHERE source_ref=?", (source_ref,)).fetchone()
    return str(row[0]) if row else None


def location_labels(conn: sqlite3.Connection, source_ref: str) -> dict[str, str]:
    return {str(row[0]): str(row[1]) for row in conn.execute("SELECT location_id,label FROM provider_locations WHERE source_ref=?", (source_ref,))}


def save_locations(conn: sqlite3.Connection, source_ref: str, locations: list[dict[str, Any]]) -> None:
    from fwrouter_api.services.events import safe_human_label
    for item in locations:
        location_id = item.get("id")
        label = safe_human_label(item.get("name") or item.get("title") or item.get("country"))
        if location_id is not None and label:
            conn.execute("INSERT INTO provider_locations VALUES (?,?,?) ON CONFLICT(source_ref,location_id) DO UPDATE SET label=excluded.label", (source_ref, str(location_id), label))
