"""Durable credential intent, separate from content-addressed subscription inventory.

Callers own the SQLite transaction and Xray writer reservation. No native state,
subscription export, or last-good snapshot is changed by these operations.
Errors deliberately contain no credentials, provider material, or source URLs.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid5


class IdentityConflict(RuntimeError):
    pass


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def profile_provenance(profile: dict[str, Any]) -> dict[str, Any]:
    outbounds = profile.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        raise IdentityConflict("IDENTITY_PROFILE_WITNESS_INVALID")
    return {
        "version": 1,
        "root": canonical_digest({key: value for key, value in profile.items()
                                  if key != "outbounds"}),
        "outbounds": [canonical_digest(value) for value in outbounds],
        "content": canonical_digest(profile),
    }


def compatible_profile_growth(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Strict, ordered retention; no name/primary-endpoint guessing.

    A deletion, transport/credential edit, or root policy edit requires an
    explicit reviewed identity assignment instead of automatic continuity.
    """
    if before.get("version") != 1 or after.get("version") != 1:
        return False
    old, new = before.get("outbounds"), after.get("outbounds")
    if before.get("root") != after.get("root") or not isinstance(old, list) or not old:
        return False
    if not isinstance(new, list) or len(old) >= len(new):
        return False
    remaining = iter(new)
    return all(any(candidate == digest for candidate in remaining) for digest in old)


def register_node(connection: Any, server_id: str) -> str:
    existing = connection.execute(
        "SELECT identity_id FROM subscription_node_aliases WHERE server_id=?", (server_id,)
    ).fetchone()
    if existing:
        return str(existing["identity_id"])
    # Opaque stable key, never a hash of subsequent content revisions.
    identity_id = str(uuid5(NAMESPACE_DNS, "fwrouter-logical-node:" + server_id))
    connection.execute(
        "INSERT INTO subscription_node_identities "
        "(identity_id, origin_server_id, current_server_id, provenance_sha256) VALUES (?, ?, ?, ?)",
        (identity_id, server_id, server_id, canonical_digest({"legacy_server": server_id})),
    )
    connection.execute("INSERT INTO subscription_node_aliases VALUES (?, ?)",
                       (server_id, identity_id))
    return identity_id


def credentials_for_scope(connection: Any, scope: str) -> dict[str, dict[str, Any]]:
    return {str(row["server_id"]): dict(row) for row in connection.execute(
        """SELECT a.server_id, c.client_uuid, c.client_email, c.epoch, c.revoked
           FROM subscription_node_aliases a JOIN subscription_node_credentials c
           ON c.identity_id=a.identity_id WHERE c.scope=?""", (scope,)
    )}


def origin_server_id_for_server(connection: Any, server_id: str) -> str:
    row = connection.execute(
        """SELECT i.origin_server_id FROM subscription_node_aliases a
           JOIN subscription_node_identities i ON i.identity_id=a.identity_id
           WHERE a.server_id=?""",
        (server_id,),
    ).fetchone()
    return str(row["origin_server_id"]) if row else server_id


def retain_credentials(connection: Any, *, scope: str, server_id: str,
                       client_uuid: str, client_email: str) -> dict[str, Any]:
    try:
        UUID(client_uuid)
    except (ValueError, TypeError, AttributeError) as exc:
        raise IdentityConflict("IDENTITY_CREDENTIAL_INVALID") from exc
    if not client_email or client_email != client_email.strip():
        raise IdentityConflict("IDENTITY_CREDENTIAL_INVALID")
    identity_id = register_node(connection, server_id)
    row = connection.execute(
        "SELECT * FROM subscription_node_credentials WHERE scope=? AND identity_id=?",
        (scope, identity_id),
    ).fetchone()
    if row:
        if (row["client_uuid"], row["client_email"]) != (client_uuid, client_email):
            raise IdentityConflict("IDENTITY_CREDENTIAL_CONFLICT")
        if row["revoked"]:
            raise IdentityConflict("IDENTITY_CREDENTIAL_REVOKED")
        return dict(row)
    duplicate = connection.execute(
        "SELECT 1 FROM subscription_node_credentials WHERE client_uuid=? OR client_email=? "
        "UNION ALL SELECT 1 FROM subscription_node_credential_history WHERE client_uuid=? LIMIT 1",
        (client_uuid, client_email, client_uuid),
    ).fetchone()
    if duplicate:
        raise IdentityConflict("IDENTITY_CREDENTIAL_OWNERSHIP_CONFLICT")
    connection.execute(
        "INSERT INTO subscription_node_credentials "
        "(scope, identity_id, client_uuid, client_email) VALUES (?, ?, ?, ?)",
        (scope, identity_id, client_uuid, client_email),
    )
    return {"client_uuid": client_uuid, "client_email": client_email, "epoch": 1, "revoked": 0}


def migrate_profile_identity(connection: Any, *, old_server_id: str, new_server_id: str,
                             old_profile: dict[str, Any], new_profile: dict[str, Any],
                             expected_revision: int,
                             allow_profile_change: bool = False,
                             operator_plan_sha256: str | None = None,
                             checkpoint_sha256: str | None = None) -> str:
    """Admit a canonical witness under the caller's writer/CAS lease.

    Automatic callers retain strict growth admission. A reviewed migration may
    explicitly allow a full-profile change; it still requires exact IDs,
    source ownership, revision CAS, and a recorded transition witness.
    """
    before, after = profile_provenance(old_profile), profile_provenance(new_profile)
    if (old_server_id != "sub:" + before["content"] or
            new_server_id != "sub:" + after["content"] or
            (not allow_profile_change and not compatible_profile_growth(before, after))):
        raise IdentityConflict("IDENTITY_MIGRATION_WITNESS_MISMATCH")
    return migrate_profile_provenance(
        connection, old_server_id=old_server_id, new_server_id=new_server_id,
        before=before, after=after, expected_revision=expected_revision,
        allow_profile_change=allow_profile_change,
        operator_plan_sha256=operator_plan_sha256, checkpoint_sha256=checkpoint_sha256,
    )


def migrate_profile_provenance(connection: Any, *, old_server_id: str, new_server_id: str,
                               before: dict[str, Any], after: dict[str, Any],
                               expected_revision: int,
                               allow_profile_change: bool = False,
                               operator_plan_sha256: str | None = None,
                               checkpoint_sha256: str | None = None) -> str:
    if (old_server_id != "sub:" + str(before.get("content") or "") or
            new_server_id != "sub:" + str(after.get("content") or "") or
            (not allow_profile_change and not compatible_profile_growth(before, after))):
        raise IdentityConflict("IDENTITY_MIGRATION_WITNESS_MISMATCH")
    before_sha = canonical_digest(before)
    after_sha = canonical_digest(after)
    replay = connection.execute(
        """SELECT identity_id FROM subscription_node_identity_transitions
           WHERE from_server_id=? AND to_server_id=? AND from_revision=?
             AND before_provenance_sha256=? AND after_provenance_sha256=?""",
        (old_server_id, new_server_id, expected_revision, before_sha, after_sha),
    ).fetchone()
    if replay:
        return str(replay["identity_id"])
    source_sets = []
    for server_id in (old_server_id, new_server_id):
        row = connection.execute("SELECT inventory_state FROM servers WHERE server_id=?",
                                 (server_id,)).fetchone()
        if not row or row["inventory_state"] == "deleted":
            raise IdentityConflict("IDENTITY_MIGRATION_SERVER_UNAVAILABLE")
        source_sets.append({str(membership["source_id"]) for membership in connection.execute(
            "SELECT source_id FROM subscription_server_memberships WHERE server_id=?",
            (server_id,),
        )})
    if not source_sets[0] or source_sets[0] != source_sets[1]:
        raise IdentityConflict("IDENTITY_MIGRATION_SOURCE_CONFLICT")
    active_old = {str(row["source_id"]) for row in connection.execute(
        "SELECT source_id FROM subscription_server_memberships WHERE server_id=? AND is_active=1",
        (old_server_id,),
    )}
    active_new = {str(row["source_id"]) for row in connection.execute(
        "SELECT source_id FROM subscription_server_memberships WHERE server_id=? AND is_active=1",
        (new_server_id,),
    )}
    if not active_new or active_old & active_new:
        raise IdentityConflict("IDENTITY_MIGRATION_ACTIVE_ALIAS_CONFLICT")
    ownership_sha = canonical_digest(sorted(source_sets[0]))
    proof = canonical_digest({"before": before_sha, "after": after_sha,
                              "source_ownership": ownership_sha})
    identity_id = register_node(connection, old_server_id)
    identity = connection.execute("SELECT * FROM subscription_node_identities WHERE identity_id=?",
                                  (identity_id,)).fetchone()
    target = connection.execute("SELECT identity_id FROM subscription_node_aliases WHERE server_id=?",
                                (new_server_id,)).fetchone()
    if target:
        if (target["identity_id"] == identity_id and identity["current_server_id"] == new_server_id
                and identity["provenance_sha256"] == proof):
            return identity_id
        raise IdentityConflict("IDENTITY_MIGRATION_TARGET_CONFLICT")
    if identity["revision"] != expected_revision or identity["current_server_id"] != old_server_id:
        raise IdentityConflict("IDENTITY_MIGRATION_REVISION_STALE")
    changed = connection.execute(
        "UPDATE subscription_node_identities SET current_server_id=?, revision=revision+1, "
        "provenance_sha256=? WHERE identity_id=? AND revision=? AND current_server_id=?",
        (new_server_id, proof, identity_id, expected_revision, old_server_id),
    ).rowcount
    if changed != 1:
        raise IdentityConflict("IDENTITY_MIGRATION_REVISION_STALE")
    connection.execute("INSERT INTO subscription_node_aliases VALUES (?, ?)",
                       (new_server_id, identity_id))
    connection.execute(
        """INSERT INTO subscription_node_identity_transitions
           (identity_id, from_server_id, to_server_id, from_revision, to_revision,
            before_provenance_sha256, after_provenance_sha256, source_ownership_sha256,
            operator_plan_sha256, checkpoint_sha256)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (identity_id, old_server_id, new_server_id, expected_revision, expected_revision + 1,
         before_sha, after_sha, ownership_sha, operator_plan_sha256, checkpoint_sha256),
    )
    return identity_id


def inherit_profile_growth(
    connection: Any, servers: list[Any],
    removed_by_source: dict[str, set[str]] | None = None,
    introduced_by_source: dict[str, set[str]] | None = None,
) -> None:
    """Refresh admission after source membership insertion, in the same transaction.

    Only parser-produced canonical witnesses are considered. Legacy rows without
    witnesses need the explicit protected migration manifest. Distinct profiles
    sharing members are never silently merged.
    """
    candidates = []
    removed_by_source = removed_by_source or {}
    introduced_by_source = introduced_by_source or {}
    # A prior JSON logical profile disappeared while one or more profiles were
    # introduced from that exact source. Preserve the source refresh, but mark
    # introduced profiles as requiring reviewed identity migration unless one
    # strict, unique ordered-growth witness proves continuity. Generation checks
    # this marker before identity registration or native staging.
    for source_id, removed_ids in removed_by_source.items():
        introduced_ids = introduced_by_source.get(source_id, set())
        if not removed_ids or not introduced_ids:
            continue
        for old_id in removed_ids:
            row = connection.execute(
                "SELECT raw_json FROM servers WHERE server_id=?", (old_id,)
            ).fetchone()
            if not row:
                continue
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, ValueError):
                continue
            topology = raw.get("_fwrouter_topology") if isinstance(raw, dict) else None
            witness = raw.get("_fwrouter_profile_provenance") if isinstance(raw, dict) else None
            if not isinstance(topology, dict) or topology.get("kind") != "logical_profile":
                continue
            introduced_profiles = [server for server in servers
                                   if server.server_id in introduced_ids
                                   and isinstance(server.raw.get("_fwrouter_profile_provenance"), dict)]
            compatible = [server for server in introduced_profiles
                          if isinstance(witness, dict)
                          and compatible_profile_growth(witness, server.raw["_fwrouter_profile_provenance"])]
            old_sources = {str(item["source_id"]) for item in connection.execute(
                "SELECT source_id FROM subscription_server_memberships WHERE server_id=?", (old_id,),
            )}
            ownership_matches = [server for server in compatible if old_sources and old_sources == {
                str(item["source_id"]) for item in connection.execute(
                    "SELECT source_id FROM subscription_server_memberships WHERE server_id=?",
                    (server.server_id,),
                )
            }]
            if len(ownership_matches) == 1:
                continue
            for server in introduced_profiles:
                server.raw["_fwrouter_identity_migration_required"] = True
                connection.execute(
                    "UPDATE servers SET raw_json=? WHERE server_id=?",
                    (json.dumps(server.raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                     server.server_id),
                )
    for row in connection.execute(
        """SELECT s.server_id, s.raw_json, i.identity_id, i.revision, i.current_server_id
           FROM servers s LEFT JOIN subscription_node_aliases a ON a.server_id=s.server_id
           LEFT JOIN subscription_node_identities i ON i.identity_id=a.identity_id
           WHERE s.inventory_state!='deleted'"""
    ):
        if row["identity_id"] and row["current_server_id"] != row["server_id"]:
            continue
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError):
            continue
        witness = raw.get("_fwrouter_profile_provenance") if isinstance(raw, dict) else None
        if isinstance(witness, dict):
            candidates.append((dict(row), witness))
    proposed: list[tuple[dict[str, Any], dict[str, Any], Any]] = []
    for server in servers:
        after = server.raw.get("_fwrouter_profile_provenance")
        if (not isinstance(after, dict)
                or server.raw.get("_fwrouter_identity_migration_required") is True):
            continue
        sources = {str(row["source_id"]) for row in connection.execute(
            "SELECT source_id FROM subscription_server_memberships WHERE server_id=?",
            (server.server_id,),
        )}
        matches = []
        for row, before in candidates:
            if row["server_id"] == server.server_id or not compatible_profile_growth(before, after):
                continue
            old_sources = {str(item["source_id"]) for item in connection.execute(
                "SELECT source_id FROM subscription_server_memberships WHERE server_id=?",
                (row["server_id"],),
            )}
            if sources and sources == old_sources:
                matches.append((row, before, server))
        if len(matches) > 1:
            raise IdentityConflict("IDENTITY_REFRESH_AMBIGUOUS")
        proposed.extend(matches)
    if len({item[0]["server_id"] for item in proposed}) != len(proposed):
        raise IdentityConflict("IDENTITY_REFRESH_AMBIGUOUS")
    for row, before, server in proposed:
        migrate_profile_provenance(
            connection, old_server_id=str(row["server_id"]), new_server_id=server.server_id,
            before=before, after=server.raw["_fwrouter_profile_provenance"],
            expected_revision=int(row["revision"] or 1), allow_profile_change=False,
        )


def rotate_credentials(connection: Any, *, scope: str, identity_id: str,
                       expected_epoch: int, operation_id: str) -> dict[str, Any]:
    """Explicit deterministic rotation, safe to replay after an interrupted caller.

    The operation ID is caller-owned intent, not derived from profile content.
    Native publication still requires the ordinary fenced generation path.
    """
    if not operation_id or len(operation_id) > 256:
        raise IdentityConflict("IDENTITY_ROTATION_OPERATION_INVALID")
    new_uuid = str(uuid5(NAMESPACE_DNS, f"fwrouter-rotation:{identity_id}:{scope}:{operation_id}"))
    row = connection.execute("SELECT * FROM subscription_node_credentials WHERE scope=? AND identity_id=?",
                             (scope, identity_id)).fetchone()
    if not row or row["revoked"]:
        raise IdentityConflict("IDENTITY_CREDENTIAL_REVOKED")
    if row["epoch"] == expected_epoch + 1 and row["client_uuid"] == new_uuid:
        return dict(row)
    if row["epoch"] != expected_epoch:
        raise IdentityConflict("IDENTITY_ROTATION_EPOCH_STALE")
    if connection.execute(
        "SELECT 1 FROM subscription_node_credentials WHERE client_uuid=? AND identity_id<>?",
        (new_uuid, identity_id),
    ).fetchone():
        raise IdentityConflict("IDENTITY_CREDENTIAL_OWNERSHIP_CONFLICT")
    if connection.execute("SELECT 1 FROM subscription_node_credential_history WHERE client_uuid=?",
                          (new_uuid,)).fetchone():
        raise IdentityConflict("IDENTITY_ROTATION_UUID_RETIRED")
    connection.execute(
        "INSERT INTO subscription_node_credential_history "
        "(client_uuid, scope, identity_id, epoch, operation_id_sha256) VALUES (?, ?, ?, ?, ?)",
        (row["client_uuid"], scope, identity_id, row["epoch"],
         hashlib.sha256(operation_id.encode()).hexdigest()),
    )
    changed = connection.execute(
        "UPDATE subscription_node_credentials SET client_uuid=?, epoch=epoch+1 "
        "WHERE scope=? AND identity_id=? AND epoch=? AND revoked=0",
        (new_uuid, scope, identity_id, expected_epoch),
    ).rowcount
    if changed != 1:
        raise IdentityConflict("IDENTITY_ROTATION_EPOCH_STALE")
    return {**dict(row), "client_uuid": new_uuid, "epoch": expected_epoch + 1}


def revoke_credentials(connection: Any, *, scope: str, identity_id: str,
                       expected_epoch: int) -> dict[str, Any]:
    """Tombstone one scoped credential; replay is safe and ownership stays reserved."""
    row = connection.execute(
        "SELECT * FROM subscription_node_credentials WHERE scope=? AND identity_id=?",
        (scope, identity_id),
    ).fetchone()
    if not row:
        raise IdentityConflict("IDENTITY_CREDENTIAL_UNAVAILABLE")
    if row["revoked"] and row["epoch"] == expected_epoch + 1:
        return dict(row)
    if row["revoked"]:
        raise IdentityConflict("IDENTITY_CREDENTIAL_REVOKED")
    if row["epoch"] != expected_epoch:
        raise IdentityConflict("IDENTITY_ROTATION_EPOCH_STALE")
    connection.execute(
        "INSERT INTO subscription_node_credential_history "
        "(client_uuid, scope, identity_id, epoch, operation_id_sha256) VALUES (?, ?, ?, ?, ?)",
        (row["client_uuid"], scope, identity_id, row["epoch"],
         hashlib.sha256(f"revoke:{scope}:{identity_id}:{expected_epoch}".encode()).hexdigest()),
    )
    changed = connection.execute(
        "UPDATE subscription_node_credentials SET revoked=1, epoch=epoch+1 "
        "WHERE scope=? AND identity_id=? AND epoch=? AND revoked=0",
        (scope, identity_id, expected_epoch),
    ).rowcount
    if changed != 1:
        raise IdentityConflict("IDENTITY_ROTATION_EPOCH_STALE")
    return {**dict(row), "revoked": 1, "epoch": expected_epoch + 1}
