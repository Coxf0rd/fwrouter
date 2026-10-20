from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fwrouter_api.services.xray_subscription import configured_xray_public_endpoint
from fwrouter_api.adapters.xray import XRAY_PUBLIC_PATH as XRAY_PUBLIC_PATH, XRAY_PUBLIC_PORT as XRAY_PUBLIC_PORT, XrayClient
from fwrouter_api.adapters.xray_common import xray_writer_guard, xray_writer_guard_is_held, xray_writer_guarded
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.auto_eligibility import auto_eligible_sql
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.jobs import JobLockConflictError
from fwrouter_api.services.events import safe_actor_identifier
from fwrouter_api.services.custom_servers import (
    VIRTUAL_CUSTOM_HTTPS_PROXY_SERVER_NAME,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
)
from fwrouter_api.services.subscription_profiles import (
    disable_subscription_identity,
    list_desired_subscription_xray_clients,
    list_subscription_profile_tokens,
    promote_runtime_verified_subscription_nodes,
    render_subscription_profile,
    _stable_digest,
)
from fwrouter_api.services.logs import write_operational_log
from fwrouter_api.services.xray_client_state import (
    _client_alias_map,
    _sync_xray_inventory,
    _xray_subject_for_client,
    cleanup_xray_client_projection,
    cleanup_xray_subscription_profile_projection,
)
from fwrouter_api.services.xray_common import (
    _materialize_xray_runtime_bindings,
    _strip_raw_payload,
    _xray_adapter,
    _xray_managed_runtime_blocked,
)
from fwrouter_api.services.xray_runtime_state import _is_xray_supported_server_config, _module_state
from fwrouter_api.services.xray_subscription import build_xray_vless_uri
from fwrouter_api.services.subject_inventory import DEFAULT_DESIRED_MODE_BY_TYPE, EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE
from fwrouter_api.services.xray_bindings import (
    _annotate_bindings_with_handoff,
    _build_binding_for_subject,
    _applied_handoff_assignments,
    collect_xray_client_mode_directives,
    collect_xray_runtime_bindings,
)
from fwrouter_api.services.xray_handoff import build_xray_handoff_assignments
import fwrouter_api.services.subject_policy as subject_policy_service


def _generation_failure_operation(result: dict[str, Any]) -> str:
    """Expose a validated leaf validator code while retaining generic result codes."""
    details = result.get("details")
    validation = details.get("validation") if isinstance(details, dict) else None
    if isinstance(validation, dict):
        for validator_name in ("local", "native"):
            validator = validation.get(validator_name)
            if not isinstance(validator, dict) or validator.get("ok") is not False:
                continue
            code = validator.get("error_code")
            if (isinstance(code, str) and 1 <= len(code) <= 96
                    and code.isascii() and all(char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in code)):
                return code
    return str(result.get("error_code") or result.get("stage") or "generation_failed")


def _legacy_xray_candidate_matches_active(active_path: Path, candidate_path: Path) -> bool:
    """Prove a private Xray candidate is unchanged or only reorders disjoint per-user rules.

    The fallback is deliberately narrower than general Xray semantic equivalence:
    every byte-level semantic field outside ``routing.rules`` must match, and
    rule movement is allowed only inside a contiguous block of exact single-user
    rules. Unknown user selectors and all non-user rules remain fixed anchors.
    """
    from collections import Counter

    max_bytes = 8 * 1024 * 1024

    def read_regular(path: Path) -> bytes | None:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                    return None
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining:
                    chunk = os.read(descriptor, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                if len(data) > max_bytes:
                    return None
                return data
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            return None

    active_raw = read_regular(Path(active_path))
    candidate_raw = read_regular(Path(candidate_path))
    if active_raw is None or candidate_raw is None:
        return False
    if active_raw == candidate_raw:
        return True

    class _InvalidJson(ValueError):
        pass

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _InvalidJson("duplicate_key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise _InvalidJson("nonfinite_number")

    try:
        active = json.loads(active_raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        candidate = json.loads(candidate_raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    if type(active) is not dict or type(candidate) is not dict:
        return False

    def canonical(value: Any) -> bytes | None:
        try:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
            return None

    active_routing = active.get("routing")
    candidate_routing = candidate.get("routing")
    if type(active_routing) is not dict or type(candidate_routing) is not dict:
        return False
    active_rules = active_routing.get("rules")
    candidate_rules = candidate_routing.get("rules")
    if type(active_rules) is not list or type(candidate_rules) is not list or len(active_rules) != len(candidate_rules):
        return False

    active_without_rules = dict(active)
    candidate_without_rules = dict(candidate)
    active_routing_without_rules = dict(active_routing)
    candidate_routing_without_rules = dict(candidate_routing)
    active_routing_without_rules["rules"] = []
    candidate_routing_without_rules["rules"] = []
    active_without_rules["routing"] = active_routing_without_rules
    candidate_without_rules["routing"] = candidate_routing_without_rules
    active_base = canonical(active_without_rules)
    candidate_base = canonical(candidate_without_rules)
    if active_base is None or candidate_base is None or active_base != candidate_base:
        return False

    from fwrouter_api.adapters.xray_common import XRAY_INBOUND_TAG

    def eligible_user(rule: Any) -> str | None:
        if type(rule) is not dict or set(rule) != {"type", "inboundTag", "user", "outboundTag"}:
            return None
        inbound = rule.get("inboundTag")
        users = rule.get("user")
        if (type(rule.get("type")) is not str or rule["type"] != "field"
                or type(inbound) is not list or len(inbound) != 1
                or type(inbound[0]) is not str or inbound[0] != XRAY_INBOUND_TAG
                or type(users) is not list or len(users) != 1
                or type(users[0]) is not str or not users[0]
                or users[0] != users[0].strip() or not users[0].isprintable()
                or type(rule.get("outboundTag")) is not str or not rule["outboundTag"]):
            return None
        return users[0]

    active_users: list[str | None] = [eligible_user(rule) for rule in active_rules]
    candidate_users: list[str | None] = [eligible_user(rule) for rule in candidate_rules]
    # Any unfamiliar rule that scopes by user could overlap a moved rule; refuse
    # semantic fallback rather than guessing about selectors or Xray precedence.
    if any(user is None and type(rule) is dict and "user" in rule
           for user, rule in [*zip(active_users, active_rules), *zip(candidate_users, candidate_rules)]):
        return False
    for users in (active_users, candidate_users):
        exact_users = [user for user in users if user is not None]
        if len({user.casefold() for user in exact_users}) != len(exact_users):
            return False

    index = 0
    while index < len(active_rules):
        if active_users[index] is None or candidate_users[index] is None:
            if active_users[index] is not None or candidate_users[index] is not None:
                return False
            active_anchor = canonical(active_rules[index])
            candidate_anchor = canonical(candidate_rules[index])
            if active_anchor is None or candidate_anchor is None or active_anchor != candidate_anchor:
                return False
            index += 1
            continue

        end = index
        while end < len(active_rules) and active_users[end] is not None and candidate_users[end] is not None:
            end += 1
        active_segment = [canonical(rule) for rule in active_rules[index:end]]
        candidate_segment = [canonical(rule) for rule in candidate_rules[index:end]]
        if any(rule is None for rule in active_segment + candidate_segment):
            return False
        if Counter(active_segment) != Counter(candidate_segment):
            return False
        index = end
    return True


XRAY_SUBSCRIPTION_PROFILE_DELETE_JOB_TYPE = "xray_subscription_profile_delete"
XRAY_SUBSCRIPTION_ACCOUNT_DELETE_REF_PREFIX = "subscription-account:"


class _SubscriptionProfileDeleteAccountMismatch(RuntimeError):
    pass


class _SubscriptionProfileCredentialRevocationFailed(RuntimeError):
    pass


def _full_xray_client_uri(client: XrayClient, *, display_name: str | None = None) -> str:
    label = display_name or client.alias or client.email or client.client_id
    return build_xray_vless_uri(
        client_uuid=client.client_uuid,
        label=label,
    )


def _vpn_auto_xray_client_email(server_id: str) -> str:
    digest = hashlib.sha1(server_id.encode("utf-8")).hexdigest()[:12]
    return f"vpn-auto-{digest}@fwrouter.local"


def _identity_projection_reference_sha256(connection: sqlite3.Connection) -> str:
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


def _admit_legacy_profile_witness(
    *, server_id: str, stored_raw: dict[str, Any], profile: dict[str, Any],
    expected_provenance: dict[str, Any],
) -> bool:
    """Prove a schema-24 JSON-profile target from its exact stored raw projection.

    Older rows predate persisted profile provenance. Reparse the operator's
    canonical witness with the current pure parser and require its complete
    normalized proxy projection to match the stored row. Only the two fields
    introduced for this migration flow may be absent/ignored.
    """
    if not isinstance(profile, dict) or server_id != "sub:" + str(expected_provenance.get("content") or ""):
        return False
    marker = stored_raw.get("_fwrouter_identity_migration_required")
    if marker is not None and marker is not True:
        return False
    if (
        stored_raw.get("_fwrouter_raw_identity_sha256") != expected_provenance.get("content")
        or stored_raw.get("_fwrouter_parser_format") != "json_profile"
        or stored_raw.get("_fwrouter_server_id") != server_id
    ):
        return False
    try:
        from fwrouter_api.adapters.subscription import _servers_from_json_profile

        parsed_servers, _diagnostics = _servers_from_json_profile(profile)
    except Exception:
        return False
    if len(parsed_servers) != 1:
        return False
    parsed = parsed_servers[0]
    if (
        parsed.server_id != server_id
        or parsed.parser_format != "json_profile"
        or hashlib.sha256(str(parsed.raw_identity or "").encode("utf-8")).hexdigest()
        != expected_provenance.get("content")
        or parsed.raw.get("_fwrouter_profile_provenance") != expected_provenance
    ):
        return False
    expected_raw = dict(parsed.raw)
    expected_raw.pop("_fwrouter_profile_provenance", None)
    expected_raw.pop("_fwrouter_identity_migration_required", None)
    normalized_stored = dict(stored_raw)
    normalized_stored.pop("_fwrouter_profile_provenance", None)
    normalized_stored.pop("_fwrouter_identity_migration_required", None)
    return normalized_stored == expected_raw


def _prepare_durable_profile_credentials(*, include_vpn_auto: bool,
                                         legacy_native_clients: list[Any] | None = None) -> dict[str, Any]:
    """Persist credential intent before taking the generation source fence.

    Adopt local durable identities, never the possibly partially applied native
    candidate. Each node's original legacy seed is retained after aliasing.
    """
    if not xray_writer_guard_is_held():
        raise RuntimeError("XRAY_IDENTITY_WRITER_REQUIRED")
    from fwrouter_api.services.subscription_node_identity import (
        register_node, retain_credentials, credentials_for_scope, IdentityConflict,
    )
    from fwrouter_api.services.subscription_profiles import (
        _subscription_servers, _subscription_email, _subscription_uuid,
    )
    servers = _subscription_servers()
    auto_servers = _vpn_auto_servers_for_xray_subscription() if include_vpn_auto else []
    server_ids = {
        str(server.get("server_id") or "")
        for server in [*servers, *auto_servers]
        if isinstance(server, dict) and str(server.get("server_id") or "")
    }
    journal: dict[str, Any] = {"identities": [], "aliases": [], "credentials": []}
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        journal["external_reference_preimage_sha256"] = _identity_projection_reference_sha256(connection)
        identity_ids_before = {
            str(row["identity_id"])
            for row in connection.execute("SELECT identity_id FROM subscription_node_identities")
        }
        aliases_before = {
            str(row["server_id"])
            for row in connection.execute("SELECT server_id FROM subscription_node_aliases")
        }
        credentials_before = {
            (str(row["scope"]), str(row["identity_id"]))
            for row in connection.execute("SELECT scope, identity_id FROM subscription_node_credentials")
        }
        if server_ids:
            placeholders = ", ".join("?" for _ in server_ids)
            for row in connection.execute(
                f"SELECT raw_json FROM servers WHERE inventory_state='active' "
                f"AND server_id IN ({placeholders})",
                tuple(sorted(server_ids)),
            ):
                try:
                    stored_raw = json.loads(row["raw_json"] or "{}")
                except (TypeError, ValueError) as exc:
                    raise IdentityConflict("IDENTITY_SERVER_PROJECTION_INVALID") from exc
                if not isinstance(stored_raw, dict):
                    raise IdentityConflict("IDENTITY_SERVER_PROJECTION_INVALID")
                if stored_raw.get("_fwrouter_identity_migration_required") is True:
                    raise IdentityConflict("IDENTITY_MIGRATION_REQUIRED")
        tokens = [str(row["token"]) for row in connection.execute(
            "SELECT c.token FROM subscription_clients c JOIN subscription_accounts a "
            "ON a.account_id=c.account_id WHERE c.enabled=1 AND a.enabled=1 ORDER BY c.token"
        )]
        persisted: dict[str, str] = {}
        for row in connection.execute(
            "SELECT json_extract(metadata_json, '$.detail.email') AS email, "
            "COALESCE(json_extract(metadata_json, '$.detail.client_uuid'), "
            "json_extract(metadata_json, '$.detail.client_id')) AS client_uuid "
            "FROM subjects WHERE implementation_kind='xray' AND is_deleted=0"
        ):
            if not row["email"] or not row["client_uuid"]:
                continue
            email, value = str(row["email"]), str(row["client_uuid"])
            if email in persisted and persisted[email] != value:
                raise IdentityConflict("IDENTITY_LOCAL_PROJECTION_CONFLICT")
            persisted[email] = value
        native_by_email: dict[str, str] = {}
        for client in legacy_native_clients or []:
            email = str(getattr(client, "email", "") or "")
            value = str(getattr(client, "client_uuid", "") or getattr(client, "client_id", "") or "")
            if not email or not value or email in persisted:
                continue
            if email in native_by_email and native_by_email[email] != value:
                raise IdentityConflict("IDENTITY_NATIVE_PROJECTION_CONFLICT")
            native_by_email[email] = value
        for email, value in native_by_email.items():
            persisted.setdefault(email, value)
        origins: dict[str, str] = {}
        active_identity_owners: dict[str, str] = {}
        for server in [*servers, *auto_servers]:
            server_id = str(server["server_id"])
            identity_id = register_node(connection, server_id)
            prior_owner = active_identity_owners.get(identity_id)
            if prior_owner is not None and prior_owner != server_id:
                raise IdentityConflict("IDENTITY_ACTIVE_ALIAS_CONFLICT")
            active_identity_owners[identity_id] = server_id
            row = connection.execute("SELECT origin_server_id FROM subscription_node_identities "
                                     "WHERE identity_id=?", (identity_id,)).fetchone()
            origins[server_id] = str(row["origin_server_id"])
        for token in tokens:
            retained = credentials_for_scope(connection, "subscription:" + token)
            for server in servers:
                server_id = str(server["server_id"])
                if server_id in retained:
                    if retained[server_id]["revoked"]:
                        continue
                    continue
                origin = origins[server_id]
                email = _subscription_email(token, origin)
                retain_credentials(connection, scope="subscription:" + token, server_id=server_id,
                                   client_email=email,
                                   client_uuid=persisted.get(email) or _subscription_uuid(token, origin))
        retained_auto = credentials_for_scope(connection, "vpn-auto")
        for server in auto_servers:
            server_id = str(server["server_id"])
            if server_id in retained_auto:
                if retained_auto[server_id]["revoked"]:
                    continue
                continue
            email = _vpn_auto_xray_client_email(origins[server_id])
            retain_credentials(connection, scope="vpn-auto", server_id=server_id,
                               client_email=email, client_uuid=persisted.get(email) or str(uuid4()))
        journal["identities"] = [
            dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_identities ORDER BY identity_id"
            ) if str(row["identity_id"]) not in identity_ids_before
        ]
        journal["aliases"] = [
            dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_aliases ORDER BY server_id"
            ) if str(row["server_id"]) not in aliases_before
        ]
        journal["credentials"] = [
            dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_credentials ORDER BY scope, identity_id"
            ) if (str(row["scope"]), str(row["identity_id"])) not in credentials_before
        ]
    return journal


def _discard_unapplied_profile_identity_intent(
    journal: dict[str, Any], *, checkpoint_path: Path,
    checkpoint_preimage_exists: bool, checkpoint_preimage_sha256: str | None,
    expected_source_fingerprint: str | None,
) -> dict[str, Any]:
    """Compensate only this invocation's identity rows before checkpoint admission.

    The caller holds the Xray writer reservation. Existing mappings are never
    included in the journal and are never modified by this compensation.
    """
    if not xray_writer_guard_is_held():
        return {"ok": False, "reason": "writer_reservation_missing"}
    identities = journal.get("identities") or []
    aliases = journal.get("aliases") or []
    credentials = journal.get("credentials") or []
    if not identities and not aliases and not credentials:
        return {"ok": True, "removed": 0}
    if not expected_source_fingerprint or (checkpoint_preimage_exists and not checkpoint_preimage_sha256):
        return {"ok": False, "reason": "source_fingerprint_unavailable"}
    try:
        current_exists = checkpoint_path.is_file()
        current_sha256 = _generation_checkpoint_sha256(checkpoint_path) if current_exists else None
        if current_exists != checkpoint_preimage_exists or current_sha256 != checkpoint_preimage_sha256:
            return {"ok": False, "reason": "checkpoint_ownership_changed"}

        identity_ids = {str(row["identity_id"]) for row in identities}
        identity_ids.update(str(row["identity_id"]) for row in aliases)
        identity_ids.update(str(row["identity_id"]) for row in credentials)
        alias_ids = {str(row["server_id"]) for row in aliases}
        credential_keys = {(str(row["scope"]), str(row["identity_id"])) for row in credentials}

        with db_session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # This read runs while the SQLite writer lock is held, so no other
            # database writer can invalidate the source proof between check and delete.
            if _generation_source_fingerprint() != expected_source_fingerprint:
                return {"ok": False, "reason": "source_fingerprint_changed"}
            current_exists = checkpoint_path.is_file()
            current_sha256 = _generation_checkpoint_sha256(checkpoint_path) if current_exists else None
            if current_exists != checkpoint_preimage_exists or current_sha256 != checkpoint_preimage_sha256:
                return {"ok": False, "reason": "checkpoint_ownership_changed"}
            expected_reference_sha256 = journal.get("external_reference_preimage_sha256")
            if (
                not isinstance(expected_reference_sha256, str)
                or len(expected_reference_sha256) != 64
                or _identity_projection_reference_sha256(connection) != expected_reference_sha256
            ):
                return {"ok": False, "reason": "identity_projection_references_changed"}

            def exact_rows(table: str, key_columns: tuple[str, ...], expected: list[dict[str, Any]]) -> bool:
                for row in expected:
                    where = " AND ".join(f"{column}=?" for column in key_columns)
                    key = tuple(row[column] for column in key_columns)
                    current = connection.execute(
                        f'SELECT * FROM "{table}" WHERE {where}', key,
                    ).fetchone()
                    if current is None or dict(current) != row:
                        return False
                return True

            if not (
                exact_rows("subscription_node_identities", ("identity_id",), identities)
                and exact_rows("subscription_node_aliases", ("server_id",), aliases)
                and exact_rows("subscription_node_credentials", ("scope", "identity_id"), credentials)
            ):
                return {"ok": False, "reason": "journal_rows_changed"}

            # Ownership checks that protect deleting an identity apply only
            # when this invocation created the identity row itself. A newly
            # added scope credential can legitimately attach to an existing
            # logical identity that already owns credentials, aliases, or
            # history; compensation removes only the exact journaled row and
            # must leave those pre-existing rows untouched.
            new_identity_ids = {str(row["identity_id"]) for row in identities}
            for identity_id in new_identity_ids:
                if connection.execute(
                    "SELECT 1 FROM subscription_node_identity_transitions WHERE identity_id=? LIMIT 1",
                    (identity_id,),
                ).fetchone():
                    return {"ok": False, "reason": "identity_transition_exists"}
                if connection.execute(
                    "SELECT 1 FROM subscription_node_credential_history WHERE identity_id=? LIMIT 1",
                    (identity_id,),
                ).fetchone():
                    return {"ok": False, "reason": "credential_history_exists"}
                actual_credentials = {
                    (str(row["scope"]), str(row["identity_id"]))
                    for row in connection.execute(
                        "SELECT scope, identity_id FROM subscription_node_credentials WHERE identity_id=?",
                        (identity_id,),
                    )
                }
                if actual_credentials - credential_keys:
                    return {"ok": False, "reason": "identity_has_unowned_credentials"}
                actual_aliases = {
                    str(row["server_id"])
                    for row in connection.execute(
                        "SELECT server_id FROM subscription_node_aliases WHERE identity_id=?",
                        (identity_id,),
                    )
                }
                if actual_aliases - alias_ids:
                    return {"ok": False, "reason": "identity_has_unowned_aliases"}

            for row in credentials:
                changed = connection.execute(
                    "DELETE FROM subscription_node_credentials WHERE scope=? AND identity_id=?",
                    (row["scope"], row["identity_id"]),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("XRAY_IDENTITY_INTENT_COMPENSATION_STALE")
            for row in aliases:
                changed = connection.execute(
                    "DELETE FROM subscription_node_aliases WHERE server_id=? AND identity_id=?",
                    (row["server_id"], row["identity_id"]),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("XRAY_IDENTITY_INTENT_COMPENSATION_STALE")
            for row in identities:
                changed = connection.execute(
                    "DELETE FROM subscription_node_identities WHERE identity_id=? AND origin_server_id=? "
                    "AND current_server_id=? AND revision=? AND provenance_sha256=? AND created_at=?",
                    (row["identity_id"], row["origin_server_id"], row["current_server_id"],
                     row["revision"], row["provenance_sha256"], row["created_at"]),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("XRAY_IDENTITY_INTENT_COMPENSATION_STALE")
        return {"ok": True, "removed": len(identities) + len(aliases) + len(credentials)}
    except Exception:
        return {"ok": False, "reason": "compensation_error"}


@xray_writer_guarded
def migrate_subscription_profile_identities(
    mappings: list[dict[str, Any]], *, expected_selection_revision: int | None = None,
    expected_identity_digest: str | None = None, apply: bool = False,
    expected_plan_sha256: str | None = None, checkpoint_sha256: str | None = None,
) -> dict[str, Any]:
    """Preview or install reviewed identity continuity without native changes.

    Preview is rollback-only. Apply requires the exact digest returned by a
    preview so source ownership, identity preimage, and selection revision are
    revalidated under the shared writer reservation.
    """
    from fwrouter_api.services.subscription_node_identity import (
        migrate_profile_identity, retain_credentials, origin_server_id_for_server,
        credentials_for_scope, IdentityConflict, canonical_digest, profile_provenance,
    )
    from fwrouter_api.services.subscription_profiles import _subscription_email
    from fwrouter_api.services.vpn_auto_selection_state import (
        read_selection_revision, advance_selection_revision,
    )
    if not mappings or len(mappings) > 256:
        raise IdentityConflict("IDENTITY_MIGRATION_MANIFEST_INVALID")
    if checkpoint_sha256 is not None and (
        len(checkpoint_sha256) != 64 or any(char not in "0123456789abcdef" for char in checkpoint_sha256)
    ):
        raise IdentityConflict("IDENTITY_MIGRATION_CHECKPOINT_DIGEST_INVALID")
    from fwrouter_api.db.connection import get_db_path
    from fwrouter_api.db.schema_state import EXPECTED_SCHEMA_VERSION, inspect_database_schema
    db_path = get_db_path()
    if not db_path.is_file():
        raise IdentityConflict("IDENTITY_DATABASE_UNAVAILABLE")
    schema_connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    schema_connection.row_factory = sqlite3.Row
    try:
        schema_state = inspect_database_schema(schema_connection)
    finally:
        schema_connection.close()
    if schema_state.get("actual_schema_version") != EXPECTED_SCHEMA_VERSION:
        raise IdentityConflict("IDENTITY_SCHEMA_UPGRADE_REQUIRED")
    checkpoint_path = _xray_generation_checkpoint_path(_xray_adapter())
    if checkpoint_path.exists() and checkpoint_sha256 is None:
        raise IdentityConflict("IDENTITY_MIGRATION_CHECKPOINT_DIGEST_REQUIRED")
    if checkpoint_sha256 is not None and not checkpoint_path.is_file():
        raise IdentityConflict("IDENTITY_MIGRATION_CHECKPOINT_UNAVAILABLE")
    if checkpoint_sha256 is not None:
        try:
            actual_checkpoint_sha256 = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise IdentityConflict("IDENTITY_MIGRATION_CHECKPOINT_UNAVAILABLE") from exc
        if actual_checkpoint_sha256 != checkpoint_sha256:
            raise IdentityConflict("IDENTITY_MIGRATION_CHECKPOINT_STALE")
    old_ids = [str(item.get("old_server_id") or "") for item in mappings]
    new_ids = [str(item.get("new_server_id") or "") for item in mappings]
    if len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids):
        raise IdentityConflict("IDENTITY_MIGRATION_AMBIGUOUS")
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_revision = read_selection_revision(connection)
        persisted: dict[str, str] = {}
        for row in connection.execute(
            "SELECT json_extract(metadata_json, '$.detail.email') AS email, "
            "COALESCE(json_extract(metadata_json, '$.detail.client_uuid'), "
            "json_extract(metadata_json, '$.detail.client_id')) AS client_uuid "
            "FROM subjects WHERE implementation_kind='xray' AND is_deleted=0 AND is_active=1"
        ):
            if row["email"] and row["client_uuid"]:
                email, value = str(row["email"]), str(row["client_uuid"])
                if email in persisted and persisted[email] != value:
                    raise IdentityConflict("IDENTITY_LOCAL_PROJECTION_CONFLICT")
                persisted[email] = value
        identity_digest = canonical_digest(sorted((value, key) for key, value in persisted.items()))
        if expected_selection_revision is not None and current_revision != expected_selection_revision:
            raise IdentityConflict("IDENTITY_MIGRATION_SELECTION_STALE")
        if expected_identity_digest is not None and identity_digest != expected_identity_digest:
            raise IdentityConflict("IDENTITY_MIGRATION_PREIMAGE_STALE")
        source_preimage = {
            "identities": [dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_identities ORDER BY identity_id")],
            "aliases": [dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_aliases ORDER BY server_id")],
            "credentials": [dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_credentials ORDER BY scope, identity_id")],
            "credential_history": [dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_credential_history ORDER BY client_uuid")],
            "memberships": [dict(row) for row in connection.execute(
                "SELECT source_id, server_id, is_active FROM subscription_server_memberships "
                "ORDER BY source_id, server_id")],
            "servers": [dict(row) for row in connection.execute(
                "SELECT server_id, inventory_state, raw_json FROM servers ORDER BY server_id")],
        }
        plan_sha256 = canonical_digest({
            "manifest_sha256": canonical_digest(mappings),
            "selection_revision": current_revision,
            "identity_preimage_sha256": identity_digest,
            "source_preimage_sha256": canonical_digest(source_preimage),
            "checkpoint_sha256": checkpoint_sha256,
        })
        if apply and not expected_plan_sha256:
            raise IdentityConflict("IDENTITY_MIGRATION_PLAN_DIGEST_REQUIRED")
        if expected_plan_sha256 is not None and expected_plan_sha256 != plan_sha256:
            raise IdentityConflict("IDENTITY_MIGRATION_PLAN_STALE")
        tokens = [str(row["token"]) for row in connection.execute(
            "SELECT c.token FROM subscription_clients c JOIN subscription_accounts a "
            "ON a.account_id=c.account_id WHERE c.enabled=1 AND a.enabled=1 ORDER BY c.token"
        )]
        changed = 0
        inventory_changed = False
        for item in mappings:
            old_id = str(item["old_server_id"])
            origin_id = origin_server_id_for_server(connection, old_id)
            target_id = str(item["new_server_id"])
            target_row = connection.execute(
                "SELECT raw_json, inventory_state FROM servers WHERE server_id=?", (target_id,),
            ).fetchone()
            if not target_row or target_row["inventory_state"] != "active":
                raise IdentityConflict("IDENTITY_MIGRATION_SERVER_UNAVAILABLE")
            target_raw = json.loads(target_row["raw_json"] or "{}")
            expected_provenance = profile_provenance(item["new_profile"])
            if "_fwrouter_profile_provenance" in target_raw:
                if target_raw.get("_fwrouter_profile_provenance") != expected_provenance:
                    raise IdentityConflict("IDENTITY_MIGRATION_STORED_WITNESS_MISMATCH")
            else:
                if not _admit_legacy_profile_witness(
                    server_id=target_id, stored_raw=target_raw,
                    profile=item["new_profile"], expected_provenance=expected_provenance,
                ):
                    raise IdentityConflict("IDENTITY_MIGRATION_STORED_WITNESS_MISMATCH")
            for scope, email in [
                *(("subscription:" + token, _subscription_email(token, origin_id)) for token in tokens),
                ("vpn-auto", _vpn_auto_xray_client_email(origin_id)),
            ]:
                retained_scope = credentials_for_scope(connection, scope)
                existing = retained_scope.get(old_id)
                if existing and existing["revoked"]:
                    continue
                if email not in persisted:
                    raise IdentityConflict("IDENTITY_MIGRATION_CREDENTIAL_PREIMAGE_MISSING")
                retain_credentials(connection, scope=scope, server_id=old_id,
                                   client_uuid=persisted[email], client_email=email)
            prior = connection.execute("SELECT identity_id FROM subscription_node_aliases WHERE server_id=?",
                                       (str(item["new_server_id"]),)).fetchone()
            migrate_profile_identity(
                connection, old_server_id=old_id, new_server_id=str(item["new_server_id"]),
                old_profile=item["old_profile"], new_profile=item["new_profile"],
                expected_revision=int(item["expected_identity_revision"]),
                allow_profile_change=True,
                operator_plan_sha256=plan_sha256,
                checkpoint_sha256=checkpoint_sha256,
            )
            target_raw.pop("_fwrouter_identity_migration_required", None)
            target_raw["_fwrouter_profile_provenance"] = expected_provenance
            target_raw_json = json.dumps(
                target_raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            inventory_changed = inventory_changed or target_raw_json != str(target_row["raw_json"] or "")
            connection.execute(
                "UPDATE servers SET raw_json=? WHERE server_id=? AND inventory_state='active'",
                (target_raw_json, target_id),
            )
            changed += int(prior is None)
        revision = current_revision
        if changed:
            revision = advance_selection_revision(connection, expected_revision=current_revision)
            if revision is None:
                raise IdentityConflict("IDENTITY_MIGRATION_SELECTION_STALE")
        if apply and inventory_changed:
            # The reviewed migration changes exact raw_json bytes covered by
            # the inventory digest. Advance its admission marker in this same
            # transaction, but never treat the new generation as applied.
            from fwrouter_api.services.subscription import _write_subscription_inventory_candidate

            _write_subscription_inventory_candidate(connection, force_pending=True)
        if not apply:
            connection.rollback()
    return {"ok": True, "status": "applied" if apply else "dry_run",
            "mappings_count": len(mappings), "changed_count": changed,
            "preserved_identity_count": len(persisted), "selection_revision": revision,
            "identity_preimage_sha256": identity_digest, "plan_sha256": plan_sha256,
            "checkpoint_sha256": checkpoint_sha256,
            "native_applied": False, "public_snapshots_changed": False,
            "fresh_generation_required": True}


def _is_subscription_profile_email(email: str) -> bool:
    return str(email or "").startswith("sub-")


def _empty_projection_cleanup() -> dict[str, Any]:
    return {
        "subject_ids": [],
        "subjects_deleted": 0,
        "server_overrides_deleted": 0,
        "user_overrides_deleted": 0,
    }


def _merge_projection_cleanups(*cleanups: dict[str, Any] | None) -> dict[str, Any]:
    merged = _empty_projection_cleanup()
    for cleanup in cleanups:
        if not cleanup:
            continue
        merged["subject_ids"] = list(
            dict.fromkeys([*merged["subject_ids"], *cleanup.get("subject_ids", [])])
        )
        merged["subjects_deleted"] += int(cleanup.get("subjects_deleted") or 0)
        merged["server_overrides_deleted"] += int(cleanup.get("server_overrides_deleted") or 0)
        merged["user_overrides_deleted"] += int(cleanup.get("user_overrides_deleted") or 0)
    return merged


def _vpn_auto_servers_for_xray_subscription() -> list[dict[str, Any]]:
    with db_session() as connection:
        vpn_auto_rows = connection.execute(
            f"""
            SELECT s.server_id, s.server_name, s.raw_json, ps.status AS ping_status, ps.last_ping_ms
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            LEFT JOIN server_ping_state AS ps ON ps.server_id = s.server_id
            WHERE {auto_eligible_sql(server_alias="s", preferences_alias="p", respect_exclusive=False)}
              AND s.server_id NOT IN (
                  SELECT server_id FROM server_custom_https_proxy
              )
            ORDER BY
              CASE WHEN ps.status = 'success' THEN 0 ELSE 1 END,
              ps.last_ping_ms,
              s.server_id
            """
        ).fetchall()
        proxy_rows = connection.execute(
            """
            SELECT s.server_id, s.server_name, s.raw_json, ps.status AS ping_status, ps.last_ping_ms
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            LEFT JOIN server_ping_state AS ps ON ps.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.manually_deleted_at, '') = ''
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()

    normal_servers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in vpn_auto_rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        supported, reason = _is_xray_supported_server_config(raw if isinstance(raw, dict) else None)
        if not supported:
            continue
        normal_servers.append(
            {
                "server_id": row["server_id"],
                "server_name": row["server_name"],
                "raw": raw,
                "ping_status": row["ping_status"],
                "last_ping_ms": row["last_ping_ms"],
                "support_reason": reason,
            }
        )
        seen_ids.add(str(row["server_id"]))

    proxy_server: dict[str, Any] | None = None
    for row in proxy_rows:
        server_id = str(row["server_id"])
        if server_id in seen_ids:
            continue
        proxy_server = {
            "server_id": server_id,
            "server_name": VIRTUAL_CUSTOM_HTTPS_PROXY_SERVER_NAME,
            "raw": {"kind": "custom_https_proxy"},
            "ping_status": row["ping_status"],
            "last_ping_ms": row["last_ping_ms"],
            "support_reason": "custom_https_proxy",
        }
        seen_ids.add(server_id)

    result: list[dict[str, Any]] = [
        {
            "server_id": VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
            "server_name": VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
            "raw": {"kind": "xray_vpn_auto"},
            "ping_status": "virtual",
            "last_ping_ms": None,
            "support_reason": "virtual_xray_vpn_auto",
        }
    ]
    if proxy_server is not None:
        result.append(proxy_server)
    result.extend(normal_servers)
    return result


def _upsert_xray_subject_server_override(
    *,
    subject_id: str,
    selected_server_id: str,
    requested_by: str,
) -> None:
    selected_until = "2099-12-31 23:59:59"

    with db_session() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(subject_server_overrides)").fetchall()
        }

        subject_exists = connection.execute(
            "SELECT 1 FROM subjects WHERE subject_id = ? AND is_deleted = 0 LIMIT 1",
            (subject_id,),
        ).fetchone()
        if subject_exists is None:
            return

        server_exists = connection.execute(
            "SELECT 1 FROM servers WHERE server_id = ? LIMIT 1",
            (selected_server_id,),
        ).fetchone()
        if server_exists is None:
            return

        existing = connection.execute(
            "SELECT 1 FROM subject_server_overrides WHERE subject_id = ? LIMIT 1",
            (subject_id,),
        ).fetchone()

        if existing is None:
            insert_values: dict[str, Any] = {}
            if "subject_id" in columns:
                insert_values["subject_id"] = subject_id
            if "selected_server_id" in columns:
                insert_values["selected_server_id"] = selected_server_id
            if "selected_until" in columns:
                insert_values["selected_until"] = selected_until
            if "requested_by" in columns:
                insert_values["requested_by"] = requested_by
            if "created_by" in columns:
                insert_values["created_by"] = requested_by
            if "updated_by" in columns:
                insert_values["updated_by"] = requested_by

            literal_columns: list[str] = []
            literal_values: list[str] = []
            if "created_at" in columns:
                literal_columns.append("created_at")
                literal_values.append("CURRENT_TIMESTAMP")
            if "updated_at" in columns:
                literal_columns.append("updated_at")
                literal_values.append("CURRENT_TIMESTAMP")

            names = list(insert_values.keys()) + literal_columns
            placeholders = ["?"] * len(insert_values) + literal_values

            connection.execute(
                f"""
                INSERT INTO subject_server_overrides ({", ".join(names)})
                VALUES ({", ".join(placeholders)})
                """,
                tuple(insert_values.values()),
            )
        else:
            assignments: list[str] = []
            params: list[Any] = []

            if "selected_server_id" in columns:
                assignments.append("selected_server_id = ?")
                params.append(selected_server_id)
            if "selected_until" in columns:
                assignments.append("selected_until = ?")
                params.append(selected_until)
            if "requested_by" in columns:
                assignments.append("requested_by = ?")
                params.append(requested_by)
            if "updated_by" in columns:
                assignments.append("updated_by = ?")
                params.append(requested_by)
            if "updated_at" in columns:
                assignments.append("updated_at = CURRENT_TIMESTAMP")

            params.append(subject_id)
            connection.execute(
                f"""
                UPDATE subject_server_overrides
                SET {", ".join(assignments)}
                WHERE subject_id = ?
                """,
                tuple(params),
            )


def _batch_materialize_xray_subject_bindings(
    nodes: list[dict[str, Any]],
    *,
    requested_by: str,
    preserve_existing_overrides: bool = False,
) -> dict[str, Any]:
    selected_until = "2099-12-31 23:59:59"
    if not nodes:
        return {"ok": True, "updated_aliases": 0, "inserted_overrides": 0, "updated_overrides": 0}
    with db_session() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(subject_server_overrides)").fetchall()}
        subjects = connection.execute(
            """
            SELECT subject_id, alias,
                   json_extract(metadata_json, '$.detail.client_id') AS client_id,
                   json_extract(metadata_json, '$.detail.client_uuid') AS client_uuid
            FROM subjects
            WHERE implementation_kind = 'xray' AND is_deleted = 0
            """
        ).fetchall()
        subject_by_identity = {
            str(value): row
            for row in subjects
            for value in (row["client_id"], row["client_uuid"])
            if value
        }
        server_ids = {
            str(row["server_id"])
            for row in connection.execute(
                "SELECT server_id FROM servers WHERE server_id IN (%s)"
                % ", ".join("?" for _ in nodes),
                tuple(str(node["server_id"]) for node in nodes),
            ).fetchall()
        }
        subject_ids = [
            str(subject_by_identity[str(node.get("client_uuid") or node.get("client_id"))]["subject_id"])
            for node in nodes
            if str(node.get("client_uuid") or node.get("client_id")) in subject_by_identity
        ]
        existing_overrides = {}
        if subject_ids:
            existing_overrides = {
                str(row["subject_id"]): row
                for row in connection.execute(
                    "SELECT * FROM subject_server_overrides WHERE subject_id IN (%s)"
                    % ", ".join("?" for _ in subject_ids),
                    tuple(subject_ids),
                ).fetchall()
            }
        updated_aliases = inserted_overrides = updated_overrides = 0
        for node in nodes:
            client_uuid = str(node.get("client_uuid") or node.get("client_id"))
            subject = subject_by_identity.get(client_uuid)
            if subject is None:
                return {
                    "ok": False,
                    "stage": "profile_subject_lookup",
                    "error_code": "XRAY_SUB_PROFILE_SUBJECT_MISSING",
                    "error_message": f"Xray subject was not created for profile client {client_uuid}.",
                    "client_uuid": client_uuid,
                    "email": node.get("client_email"),
                }
            subject_id = str(subject["subject_id"])
            alias = str(node["xray_alias"]).strip() or None
            if subject["alias"] != alias:
                connection.execute(
                    "UPDATE subjects SET alias = ?, updated_at = CURRENT_TIMESTAMP WHERE subject_id = ?",
                    (alias, subject_id),
                )
                updated_aliases += 1
            server_id = str(node["server_id"])
            if server_id not in server_ids:
                continue
            existing = existing_overrides.get(subject_id)
            if existing is None:
                insert_values = {name: value for name, value in {
                    "subject_id": subject_id,
                    "selected_server_id": server_id,
                    "selected_until": selected_until,
                    "requested_by": requested_by,
                    "created_by": requested_by,
                    "updated_by": requested_by,
                }.items() if name in columns}
                literal_columns = [name for name in ("created_at", "updated_at") if name in columns]
                names = [*insert_values, *literal_columns]
                values = ["?"] * len(insert_values) + ["CURRENT_TIMESTAMP"] * len(literal_columns)
                connection.execute(
                    f"INSERT INTO subject_server_overrides ({', '.join(names)}) "
                    f"VALUES ({', '.join(values)})",
                    tuple(insert_values.values()),
                )
                inserted_overrides += 1
                continue
            if preserve_existing_overrides:
                continue
            semantic_values = (
                ("selected_server_id", server_id),
                ("selected_until", selected_until),
                ("requested_by", requested_by),
                ("updated_by", requested_by),
            )
            semantic_changed = any(
                name in columns and existing[name] != value
                for name, value in semantic_values
            )
            if not semantic_changed:
                continue
            assignments = []
            params = []
            for name, value in semantic_values:
                if name in columns:
                    assignments.append(f"{name} = ?")
                    params.append(value)
            if "updated_at" in columns:
                assignments.append("updated_at = CURRENT_TIMESTAMP")
            params.append(subject_id)
            connection.execute(
                f"UPDATE subject_server_overrides SET {', '.join(assignments)} "
                "WHERE subject_id = ?",
                tuple(params),
            )
            updated_overrides += 1
    return {
        "ok": True,
        "updated_aliases": updated_aliases,
        "inserted_overrides": inserted_overrides,
        "updated_overrides": updated_overrides,
    }


def _prospective_profile_bindings(
    nodes: list[dict[str, Any]],
    *,
    token_prefix: str,
    managed_email_prefixes: list[str] | None = None,
    preserve_existing_overrides: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build candidate bindings and modes without creating runtime projections."""
    prefixes = [str(value).lower() for value in (managed_email_prefixes or [token_prefix]) if value]
    scoped_email = lambda item: any(str(item.get("client_email") or "").lower().startswith(prefix) for prefix in prefixes)
    bindings = [
        item for item in collect_xray_runtime_bindings(exclude_client_email_prefixes=prefixes)
        if not scoped_email(item)
    ]
    modes = [
        item for item in collect_xray_client_mode_directives(exclude_client_email_prefixes=prefixes)
        if not scoped_email(item)
    ]
    routing = subject_policy_service.get_routing_snapshot()
    enforcement = subject_policy_service.build_runtime_enforcement_state()
    bypass = subject_policy_service.get_core_bypass_state()

    for node in nodes:
        client_uuid = str(node.get("client_uuid") or "").strip()
        email = str(node.get("client_email") or "").strip()
        if not client_uuid or not email:
            continue
        # This planner already owns routing/enforcement/bypass snapshots.
        # Resolving a fully enriched subject here repeats native preflight per
        # client, then discards it below; retain only persisted identity intent.
        current = _xray_subject_for_client(client_uuid, include_effective_state=False)
        if isinstance(current, dict):
            subject_id = str(current.get("subject_id") or f"xray:{client_uuid}")
            subject = current
        else:
            subject_id = f"xray:{client_uuid}"
            subject = {
                "subject_id": subject_id,
                "subject_type": "explicit_external_client",
                "subject_role": "vless_client",
                "implementation_kind": "xray",
                "desired_mode": DEFAULT_DESIRED_MODE_BY_TYPE[EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE],
                "is_active": 1,
                "is_deleted": 0,
                "alias": str(node.get("xray_alias") or ""),
                "detail": {
                    "client_id": client_uuid,
                    "client_uuid": client_uuid,
                    "email": email,
                    "enabled": True,
                },
            }
        user_override = subject_policy_service._load_active_user_override(subject_id)
        server_override = subject_policy_service._load_active_server_override(subject_id)
        with db_session() as connection:
            persisted_server_override = connection.execute(
                "SELECT 1 FROM subject_server_overrides WHERE subject_id = ? LIMIT 1",
                (subject_id,),
            ).fetchone() is not None
            requested_server_exists = connection.execute(
                "SELECT 1 FROM servers WHERE server_id = ? LIMIT 1",
                (str(node.get("server_id") or ""),),
            ).fetchone() is not None
        if requested_server_exists and (
            not preserve_existing_overrides
            or (server_override is None and not persisted_server_override)
        ):
            server_override = {
                "subject_id": subject_id,
                "selected_server_id": str(node.get("server_id") or ""),
                "selected_until": "2099-12-31 23:59:59",
                "apply_state": "pending",
            }
        enriched = subject_policy_service.enrich_subject_with_effective_state(
            subject,
            routing=routing,
            user_override=user_override,
            server_override=server_override,
            runtime_enforcement=enforcement,
            bypass_state=bypass,
        )
        mode = str((enriched.get("effective_state") or {}).get("effective_mode") or "").lower()
        if mode in {"direct", "disabled", "selective"}:
            modes.append({
                "subject_id": subject_id,
                "client_id": client_uuid,
                "client_uuid": client_uuid,
                "client_email": email,
                "desired_mode": mode,
                "effective_mode": "unsupported_selective" if mode == "selective" else mode,
                "mode_support_state": "unsupported_legacy" if mode == "selective" else "supported",
            })
            continue
        binding = _build_binding_for_subject(enriched)
        if binding is not None:
            bindings.append(binding)

    old_assignments = _applied_handoff_assignments()
    bindings = _annotate_bindings_with_handoff(bindings)
    assignments = build_xray_handoff_assignments(bindings, preserve_assignments=old_assignments)
    return bindings, modes, assignments


_FIXED_XRAY_ROUTING_FIELDS = (
    "desired_mode", "applied_mode", "selective_default", "server_mode",
    "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until", "apply_state",
)


def _fixed_xray_routing_projection(routing: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(routing, dict):
        return None
    return {key: routing.get(key) for key in _FIXED_XRAY_ROUTING_FIELDS}


def _fixed_xray_routing_snapshot_matches(
    snapshot: dict[str, Any] | None,
    workflow_revision: int | None,
    *, expected_selection_revision: int | None = None,
    origin_selection_revision: int | None = None,
) -> bool:
    """Rebind fixed candidate projection to the live route and owned workflow."""
    if not isinstance(snapshot, dict) or type(workflow_revision) is not int:
        return False
    try:
        from fwrouter_api.services.server_state import get_routing_global_state
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
        from fwrouter_api.services.core_intent import read_core_intent_revision

        routing = get_routing_global_state(expire_ttl=False)
        if not isinstance(routing, dict):
            return False
        if any(routing.get(key) != snapshot.get(key) for key in _FIXED_XRAY_ROUTING_FIELDS):
            return False
        if (routing.get("server_mode") != "fixed"
                or not routing.get("desired_fixed_server_id")
                or routing.get("applied_fixed_server_id") != routing.get("desired_fixed_server_id")
                or routing.get("apply_state") != "clean"):
            return False
        with db_session() as connection:
            pending = _load_row(connection)
            current_selection_revision = read_selection_revision(connection)
            core_revision = read_core_intent_revision(connection)
            core_row = connection.execute(
                "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
            ).fetchone()
            fixed_ttl_valid = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
        workflow = pending.get("workflow") if isinstance(pending, dict) else None
        if (type(expected_selection_revision) is not int
                or current_selection_revision != expected_selection_revision
                or type(origin_selection_revision) is not int
                or fixed_ttl_valid is None or not bool(fixed_ttl_valid[0])):
            return False
        if (not isinstance(workflow, dict)
                or workflow.get("selection_revision") != origin_selection_revision):
            return False
        if (core_row is None
                or tuple(str(core_row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
                    != tuple(workflow.get("core_snapshot") or ())
                or core_revision != workflow.get("core_revision")):
            return False
        core_proof = _collect_legacy_admission_core_native_proof()
        return bool(
            isinstance(pending, dict) and pending.get("pending") is True
            and pending.get("revision") == workflow_revision
            and isinstance(workflow, dict) and workflow.get("kind") == "global_fixed_server"
            and workflow.get("phase") == "core_verified"
            and workflow.get("server_id") == snapshot.get("desired_fixed_server_id")
            and workflow.get("fixed_until") == snapshot.get("fixed_server_until")
            and core_proof.get("ok") is True
            and core_proof.get("apply_id") == workflow.get("core_apply_id")
            and core_proof.get("manifest_hash") == workflow.get("core_manifest_hash")
            and core_proof.get("enforcement_level") == workflow.get("core_enforcement_level")
            and core_proof.get("live_global_mode") == workflow.get("core_live_mode")
        )
    except Exception:
        return False


def _capture_ordinary_fixed_xray_context() -> tuple[dict[str, Any] | None, str | None]:
    """Capture fixed intent for an ordinary Xray generation without claiming a workflow."""
    try:
        selection = _capture_generation_auto_selection()
        routing = selection.get("routing") if isinstance(selection, dict) else None
        if not isinstance(routing, dict) or routing.get("server_mode") != "fixed":
            return None, None
        target = str(routing.get("desired_fixed_server_id") or "").strip()
        if not target:
            # Preserve the existing targetless direct compatibility behavior.
            return None, None
        if (
            routing.get("applied_fixed_server_id") != target
            or routing.get("apply_state") != "clean"
            or routing.get("desired_mode") != routing.get("applied_mode")
        ):
            return None, "fixed_routing_state_unverified"
        with db_session() as connection:
            ttl_valid = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            core_unavailable = _legacy_admission_core_unavailable_reason(connection)
        if ttl_valid is None or not bool(ttl_valid[0]):
            return None, "fixed_server_ttl_unverified"
        if core_unavailable is not None:
            return None, core_unavailable
        core_runtime_fingerprint = _legacy_admission_core_runtime_fingerprint()
        core_proof = _collect_legacy_admission_core_native_proof()
        if core_proof.get("ok") is not True:
            return None, str(core_proof.get("reason") or "core_native_enforcement_unproven")
        selection_after_proof = _capture_generation_auto_selection()
        with db_session() as connection:
            ttl_after_proof = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            core_after_proof = _legacy_admission_core_unavailable_reason(connection)
            core_runtime_after_proof = _legacy_admission_core_runtime_fingerprint(connection)
        if (
            selection_after_proof != selection
            or ttl_after_proof is None or not bool(ttl_after_proof[0])
            or core_after_proof is not None
            or core_runtime_after_proof != core_runtime_fingerprint
        ):
            return None, "fixed_generation_context_changed_during_proof"
        return {
            "routing_snapshot": _fixed_xray_routing_projection(routing),
            "selection_snapshot": selection,
            "core_runtime_fingerprint": core_runtime_fingerprint,
            "core_proof": {
                key: core_proof.get(key) for key in (
                    "manifest_hash", "apply_id", "enforcement_level", "live_global_mode",
                )
            },
        }, None
    except Exception:
        return None, "fixed_generation_context_unavailable"


def _ordinary_fixed_xray_context_matches(
    context: dict[str, Any] | None, *, expected_selection_revision: int | None = None,
) -> bool:
    """Reprove durable fixed routing and its existing Core owner before/after each effect."""
    if not isinstance(context, dict):
        return False
    try:
        snapshot = context.get("routing_snapshot")
        selection_before = context.get("selection_snapshot")
        core_expected = context.get("core_proof")
        if not isinstance(snapshot, dict) or not isinstance(selection_before, dict) or not isinstance(core_expected, dict):
            return False
        current_selection = _capture_generation_auto_selection()
        revision = expected_selection_revision
        if revision is None:
            revision = selection_before.get("selection_revision")
        if (
            type(revision) is not int
            or current_selection.get("selection_revision") != revision
            or current_selection.get("selection_decision_id") != selection_before.get("selection_decision_id")
            or _fixed_xray_routing_projection(current_selection.get("routing")) != snapshot
        ):
            return False
        source_fingerprint = context.get("source_fingerprint")
        if (not isinstance(source_fingerprint, str) or len(source_fingerprint) != 64
                or _generation_source_fingerprint() != source_fingerprint):
            return False
        with db_session() as connection:
            if (
                _legacy_admission_core_unavailable_reason(connection) is not None
                or _legacy_admission_core_runtime_fingerprint(connection)
                != context.get("core_runtime_fingerprint")
            ):
                return False
            ttl_valid = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            if ttl_valid is None or not bool(ttl_valid[0]):
                return False
        proof = _collect_legacy_admission_core_native_proof()
        if proof.get("ok") is not True or not all(
            proof.get(key) == core_expected.get(key)
            for key in ("manifest_hash", "apply_id", "enforcement_level", "live_global_mode")
        ):
            return False
        selection_after_proof = _capture_generation_auto_selection()
        with db_session() as connection:
            ttl_after_proof = connection.execute(
                "SELECT fixed_server_until IS NOT NULL AND datetime(fixed_server_until) > CURRENT_TIMESTAMP "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            core_after_proof = _legacy_admission_core_unavailable_reason(connection)
            core_runtime_after_proof = _legacy_admission_core_runtime_fingerprint(connection)
        return bool(
            selection_after_proof.get("selection_revision") == revision
            and selection_after_proof.get("selection_decision_id") == selection_before.get("selection_decision_id")
            and _fixed_xray_routing_projection(selection_after_proof.get("routing")) == snapshot
            and ttl_after_proof is not None and bool(ttl_after_proof[0])
            and core_after_proof is None
            and core_runtime_after_proof == context.get("core_runtime_fingerprint")
            and _generation_source_fingerprint() == source_fingerprint
        )
    except Exception:
        return False


def _fixed_xray_generation_context_matches(
    routing_snapshot: dict[str, Any] | None,
    fixed_workflow_revision: int | None,
    *,
    expected_selection_revision: int | None = None,
    fixed_origin_selection_revision: int | None = None,
    ordinary_fixed_context: dict[str, Any] | None = None,
) -> bool:
    if fixed_workflow_revision is not None:
        return _fixed_xray_routing_snapshot_matches(
            routing_snapshot, fixed_workflow_revision,
            expected_selection_revision=expected_selection_revision,
            origin_selection_revision=fixed_origin_selection_revision,
        )
    if ordinary_fixed_context is not None:
        return (
            routing_snapshot == ordinary_fixed_context.get("routing_snapshot")
            and _ordinary_fixed_xray_context_matches(
                ordinary_fixed_context, expected_selection_revision=expected_selection_revision,
            )
        )
    return True


def _stage_profile_native_candidates(
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
        if (
            context_before is None
            or candidate_input_context() != context_before
            or (name == "final" and transition_context != context_before)
        ):
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


def _apply_staged_mihomo_candidate(
    candidate_path: str, expected_sha256: str, *, inventory_admission: Any = None,
    candidate_input_fingerprint: dict[str, Any] | None = None,
    fixed_routing_snapshot: dict[str, Any] | None = None,
    fixed_workflow_revision: int | None = None,
    fixed_origin_selection_revision: int | None = None,
    expected_selection_revision: int | None = None,
    ordinary_fixed_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (fixed_workflow_revision is not None
            and not _fixed_xray_routing_snapshot_matches(
                fixed_routing_snapshot, fixed_workflow_revision,
                expected_selection_revision=expected_selection_revision,
                origin_selection_revision=fixed_origin_selection_revision,
            )):
        return {"ok": False, "stage": "fixed_routing_snapshot",
                "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE", "last_good_retained": True}
    if ordinary_fixed_context is not None and not _fixed_xray_generation_context_matches(
            fixed_routing_snapshot, None,
            expected_selection_revision=expected_selection_revision,
            ordinary_fixed_context=ordinary_fixed_context):
        return {"ok": False, "stage": "fixed_routing_snapshot",
                "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE", "last_good_retained": True}
    source = Path(candidate_path)
    if not source.exists() or hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
        return {"ok": False, "stage": "candidate_integrity", "error_code": "MIHOMO_STAGE_CANDIDATE_CHANGED_AFTER_VALIDATION"}
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.artifacts import atomic_copy_file
    from fwrouter_api.services.mihomo_runtime import restart_mihomo_container
    from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint
    expected_input_hash = (
        candidate_input_fingerprint.get("hash")
        if isinstance(candidate_input_fingerprint, dict) else None
    )
    input_routing = fixed_routing_snapshot if isinstance(fixed_routing_snapshot, dict) else None
    try:
        promotion_fingerprint = (
            current_mihomo_input_fingerprint(input_routing)
            if isinstance(input_routing, dict) else current_mihomo_input_fingerprint()
        )
        current_input_hash = promotion_fingerprint.get("hash")
    except Exception:
        promotion_fingerprint = None
        current_input_hash = None
    if (
        not isinstance(expected_input_hash, str)
        or len(expected_input_hash) != 64
        or current_input_hash != expected_input_hash
    ):
        return {"ok": False, "stage": "candidate_provenance",
                "error_code": "MIHOMO_CANDIDATE_PROVENANCE_UNVERIFIED",
                "last_good_retained": True}

    active_path = Path(mihomo_config._resolved_base_config_path())
    if active_path.exists() and hashlib.sha256(active_path.read_bytes()).hexdigest() == expected_sha256:
        unchanged_revision_ok = (
            fixed_workflow_revision is None and ordinary_fixed_context is None
            or (type(expected_selection_revision) is int
                and _fixed_xray_routing_snapshot_matches(
                    fixed_routing_snapshot, fixed_workflow_revision,
                    expected_selection_revision=expected_selection_revision,
                    origin_selection_revision=fixed_origin_selection_revision,
                ))
        )
        if ordinary_fixed_context is not None:
            unchanged_revision_ok = _fixed_xray_generation_context_matches(
                fixed_routing_snapshot, None,
                expected_selection_revision=expected_selection_revision,
                ordinary_fixed_context=ordinary_fixed_context)
        return {
            "ok": unchanged_revision_ok,
            "stage": "unchanged" if unchanged_revision_ok else "fixed_routing_snapshot",
            "error_code": None if unchanged_revision_ok else "FIXED_XRAY_SELECTION_REVISION_UNVERIFIED",
            "selection_revision": expected_selection_revision,
            "container": {"ok": unchanged_revision_ok, "action": "none", "reason": "config_unchanged"},
        }
    if not _fixed_xray_generation_context_matches(
            fixed_routing_snapshot, fixed_workflow_revision,
            expected_selection_revision=(
                expected_selection_revision
                if fixed_workflow_revision is not None or ordinary_fixed_context is not None
                else None
            ),
            fixed_origin_selection_revision=fixed_origin_selection_revision,
            ordinary_fixed_context=ordinary_fixed_context):
        return {"ok": False, "stage": "fixed_routing_snapshot",
                "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE",
                "runtime_verified": False, "last_good_retained": True}
    active_candidate = Path(mihomo_config._resolved_candidate_config_path())
    atomic_copy_file(source, active_candidate)
    if hashlib.sha256(active_candidate.read_bytes()).hexdigest() != expected_sha256:
        return {"ok": False, "stage": "candidate_integrity", "error_code": "MIHOMO_STAGE_CANDIDATE_COPY_MISMATCH"}
    if inventory_admission is None:
        from fwrouter_api.services.mihomo_reconcile_fingerprint import record_candidate_provenance
        if not record_candidate_provenance(
            candidate_path=active_candidate,
            fingerprint=promotion_fingerprint or {},
        ):
            return {"ok": False, "stage": "candidate_provenance",
                    "error_code": "MIHOMO_CANDIDATE_PROVENANCE_UNVERIFIED",
                    "last_good_retained": True}
    if not _fixed_xray_generation_context_matches(
            fixed_routing_snapshot, fixed_workflow_revision,
            expected_selection_revision=(
                expected_selection_revision
                if fixed_workflow_revision is not None or ordinary_fixed_context is not None
                else None
            ),
            fixed_origin_selection_revision=fixed_origin_selection_revision,
            ordinary_fixed_context=ordinary_fixed_context):
        return {"ok": False, "stage": "fixed_routing_snapshot",
                "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE",
                "runtime_verified": False, "last_good_retained": True}
    promotion_kwargs = {
        "_inventory_admission": inventory_admission,
        "_candidate_input_fingerprint": promotion_fingerprint,
    }
    if ordinary_fixed_context is not None:
        promotion_kwargs.update({
            "selection_fenced": True,
            "expected_selection_revision": expected_selection_revision,
        })
    promoted = mihomo_config.promote_mihomo_candidate_config(**promotion_kwargs)
    if not promoted.get("ok"):
        return {"ok": False, "stage": "promote", "promoted": _strip_raw_payload(promoted)}
    promoted_revision = (
        expected_selection_revision if ordinary_fixed_context is not None
        else promoted.get("selection_revision")
    )
    if ordinary_fixed_context is not None and (
            type(promoted_revision) is not int
            or promoted_revision != expected_selection_revision
            or not _fixed_xray_generation_context_matches(
                fixed_routing_snapshot, None,
                expected_selection_revision=expected_selection_revision,
                ordinary_fixed_context=ordinary_fixed_context)):
        return {"ok": False, "stage": "selection_revision_receipt",
                "error_code": "FIXED_XRAY_SELECTION_REVISION_UNVERIFIED",
                "selection_revision": promoted_revision, "runtime_verified": False}
    if (fixed_workflow_revision is not None
            and (type(promoted_revision) is not int or not _fixed_xray_routing_snapshot_matches(
                fixed_routing_snapshot, fixed_workflow_revision,
                expected_selection_revision=promoted_revision,
                origin_selection_revision=fixed_origin_selection_revision,
            ))):
        return {"ok": False, "stage": "selection_revision_receipt",
                "error_code": "FIXED_XRAY_SELECTION_REVISION_UNVERIFIED",
                "selection_revision": promoted_revision, "runtime_verified": False}
    if fixed_workflow_revision is not None or ordinary_fixed_context is not None:
        restarted = restart_mihomo_container(
            action="force_recreate", selection_fenced=True,
            expected_selection_revision=(
                promoted_revision if fixed_workflow_revision is not None else expected_selection_revision
            ),
        )
    else:
        restarted = restart_mihomo_container(action="force_recreate")
    fixed_context_ok = fixed_workflow_revision is None and ordinary_fixed_context is None
    if fixed_workflow_revision is not None:
        fixed_context_ok = _fixed_xray_routing_snapshot_matches(
            fixed_routing_snapshot, fixed_workflow_revision,
            expected_selection_revision=promoted_revision,
            origin_selection_revision=fixed_origin_selection_revision,
        )
    if ordinary_fixed_context is not None:
        fixed_context_ok = _fixed_xray_generation_context_matches(
            fixed_routing_snapshot, None,
            expected_selection_revision=expected_selection_revision,
            ordinary_fixed_context=ordinary_fixed_context)
    return {
        "ok": bool(restarted.get("ok")) and fixed_context_ok,
        "stage": "applied" if restarted.get("ok") and fixed_context_ok else "restart",
        "error_code": None if fixed_context_ok else "FIXED_XRAY_ROUTING_SNAPSHOT_STALE",
        "selection_revision": (
            (promoted_revision if fixed_workflow_revision is not None else expected_selection_revision)
            if (fixed_workflow_revision is not None or ordinary_fixed_context is not None)
            else restarted.get("selection_revision")
        ),
        "promoted": _strip_raw_payload(promoted),
        "container": _strip_raw_payload(restarted),
        **({"runtime_verified": False} if (fixed_workflow_revision is not None or ordinary_fixed_context is not None)
           and (not restarted.get("ok") or not fixed_context_ok) else {}),
    }


def _xray_generation_checkpoint_path(adapter: Any) -> Path:
    from fwrouter_api.adapters.xray_common import _default_xray_config_path
    configured_path = getattr(adapter, "config_path", None) or _default_xray_config_path()
    return Path(configured_path).parent / ".generation" / "generation-checkpoint.json"


def _generation_checkpoint_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity_migration_ledger_matches(*, plan_sha256: str, checkpoint_sha256: str) -> int:
    with db_session() as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS count FROM subscription_node_identity_transitions
               WHERE operator_plan_sha256=? AND checkpoint_sha256=?""",
            (plan_sha256, checkpoint_sha256),
        ).fetchone()
    return int(row["count"] or 0)


def _checkpoint_supersession_state(
    adapter: Any, checkpoint_path: Path, *, checkpoint_sha256: str, plan_sha256: str,
) -> dict[str, Any]:
    from fwrouter_api.services.subscription_node_identity import canonical_digest
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    raw = checkpoint_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != checkpoint_sha256:
        raise RuntimeError("XRAY_SUPERSESSION_CHECKPOINT_STALE")
    if _identity_migration_ledger_matches(plan_sha256=plan_sha256,
                                          checkpoint_sha256=checkpoint_sha256) <= 0:
        raise RuntimeError("XRAY_SUPERSESSION_MIGRATION_PROOF_MISSING")
    config_path = Path(adapter.config_path)
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    xray_config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    xray_mounted_sha = adapter.get_runtime_config_sha256()
    if not xray_mounted_sha or xray_mounted_sha != xray_config_sha:
        raise RuntimeError("XRAY_SUPERSESSION_MOUNTED_CONFIG_MISMATCH")
    with db_session() as connection:
        selection_fence = read_selection_fence(connection)
    projection = _current_xray_projection_snapshot()
    loaded_identities = sorted(adapter.list_loaded_client_identities())
    mihomo_incarnation = get_mihomo_runtime_incarnation()
    xray_incarnation = adapter.get_runtime_incarnation()
    if not mihomo_incarnation or not xray_incarnation:
        raise RuntimeError("XRAY_SUPERSESSION_RUNTIME_INCARNATION_UNAVAILABLE")
    return {
        "checkpoint_sha256": checkpoint_sha256,
        "plan_sha256": plan_sha256,
        "source_fingerprint": _generation_source_fingerprint(),
        "selection_fence": selection_fence,
        "mihomo_config_sha256": hashlib.sha256(mihomo_path.read_bytes()).hexdigest(),
        "mihomo_runtime_incarnation": mihomo_incarnation,
        "xray_config_sha256": xray_config_sha,
        "xray_mounted_config_sha256": xray_mounted_sha,
        "xray_runtime_incarnation": xray_incarnation,
        "database_identity_sha256": canonical_digest(projection["identities"]),
        "database_identity_count": len(projection["identities"]),
        "loaded_identity_sha256": canonical_digest(loaded_identities),
        "loaded_identity_count": len(loaded_identities),
        "rollback_preimage_projection_matches": loaded_identities == projection["identities"],
    }


def _checkpoint_supersession_state_unchanged(
    adapter: Any, checkpoint_path: Path, baseline: dict[str, Any],
) -> bool:
    try:
        current = _checkpoint_supersession_state(
            adapter, checkpoint_path,
            checkpoint_sha256=str(baseline["checkpoint_sha256"]),
            plan_sha256=str(baseline["plan_sha256"]),
        )
        return current == baseline
    except Exception:
        return False


def _archive_checkpoint_exact(checkpoint_path: Path, checkpoint_sha256: str) -> Path:
    raw = checkpoint_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != checkpoint_sha256:
        raise RuntimeError("XRAY_SUPERSESSION_CHECKPOINT_STALE")
    archive_dir = checkpoint_path.parent / "checkpoint-archive"
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise RuntimeError("XRAY_SUPERSESSION_ARCHIVE_PATH_INVALID")
    archive_dir.chmod(0o700)
    archive_info = archive_dir.lstat()
    if archive_info.st_uid != os.geteuid() or stat.S_IMODE(archive_info.st_mode) != 0o700:
        raise RuntimeError("XRAY_SUPERSESSION_ARCHIVE_PATH_INVALID")
    target = archive_dir / f"generation-checkpoint-{checkpoint_sha256}.json"
    temporary = archive_dir / f".archive-{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(temporary, flags, 0o600)
            _write_all_fd(descriptor, raw)
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                owned_descriptor, descriptor = descriptor, None
                os.close(owned_descriptor)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            info = target.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or target.read_bytes() != raw):
                raise RuntimeError("XRAY_SUPERSESSION_ARCHIVE_CONFLICT")
        _fsync_directory(archive_dir)
        return target
    finally:
        if descriptor is not None:
            owned_descriptor, descriptor = descriptor, None
            try:
                os.close(owned_descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all_fd(descriptor: int, data: bytes) -> None:
    """Write bytes to an already-owned descriptor without reopening it via fdopen."""
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(descriptor, remaining[:1024 * 1024])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("short write to private checkpoint")
        remaining = remaining[written:]


def _atomic_write_private_checkpoint(path: Path, text: str) -> None:
    """Replace checkpoint bytes atomically with mode 0600 before publication."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _write_all_fd(descriptor, text.encode("utf-8"))
        os.fsync(descriptor)
        owned_descriptor, descriptor = descriptor, None
        os.close(owned_descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            owned_descriptor, descriptor = descriptor, None
            try:
                os.close(owned_descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _generation_source_fingerprint(connection: sqlite3.Connection | None = None) -> str:
    def read_sources(active: sqlite3.Connection) -> list[Any]:
        return [
            [dict(row) for row in active.execute("SELECT * FROM subscription_node_identities ORDER BY identity_id")],
            [dict(row) for row in active.execute("SELECT * FROM subscription_node_aliases ORDER BY server_id")],
            [dict(row) for row in active.execute("SELECT * FROM subscription_node_credentials ORDER BY scope, identity_id")],
            [dict(row) for row in active.execute("SELECT * FROM subscription_node_credential_history ORDER BY client_uuid")],
            [dict(row) for row in active.execute(
                "SELECT * FROM subscription_node_identity_transitions ORDER BY identity_id, from_revision"
            )],
            [dict(row) for row in active.execute("SELECT source_id, server_id, is_active FROM subscription_server_memberships ORDER BY source_id, server_id")],
            [dict(row) for row in active.execute("SELECT account_id, slug, enabled FROM subscription_accounts ORDER BY account_id").fetchall()],
            [dict(row) for row in active.execute("SELECT client_id, account_id, token, enabled FROM subscription_clients ORDER BY client_id").fetchall()],
            [dict(row) for row in active.execute("SELECT server_id, vpn_auto, vpn_auto_priority, manually_deleted_at FROM server_preferences ORDER BY server_id").fetchall()],
            [dict(row) for row in active.execute("SELECT server_id, inventory_state, raw_json FROM servers ORDER BY server_id").fetchall()],
            [dict(row) for row in active.execute("""SELECT subject_id, subject_type, subject_role, desired_mode, is_active, is_deleted,
                json_extract(metadata_json, '$.detail.client_id') AS client_id,
                json_extract(metadata_json, '$.detail.client_uuid') AS client_uuid,
                json_extract(metadata_json, '$.detail.email') AS email
                FROM subjects WHERE implementation_kind = 'xray' ORDER BY subject_id""").fetchall()],
            [dict(row) for row in active.execute("SELECT subject_id, selected_server_id, selected_until FROM subject_server_overrides ORDER BY subject_id").fetchall()],
            [dict(row) for row in active.execute("SELECT subject_id, override_mode, override_until FROM subject_user_overrides ORDER BY subject_id").fetchall()],
            [dict(row) for row in active.execute("SELECT desired_mode, selective_default, server_mode FROM routing_global_state ORDER BY id").fetchall()],
            [dict(row) for row in active.execute("SELECT key, value_json FROM settings WHERE key='vpn_auto_exclusive_source_ref'").fetchall()],
            [dict(row) for row in active.execute("SELECT source_ref, enabled, logical_server_id, provider_id, resource_id, protocol, binding_revision FROM provider_bindings ORDER BY source_ref").fetchall()],
            [dict(row) for row in active.execute("SELECT source_ref, provider_member_id, location_id, protocol, provider_status, auto_enabled, priority, advertised FROM provider_members ORDER BY source_ref, provider_member_id, location_id, protocol").fetchall()],
        ]

    if connection is None:
        with db_session() as active:
            sources = read_sources(active)
    else:
        sources = read_sources(connection)
    return hashlib.sha256(json.dumps(sources, sort_keys=True, default=str).encode()).hexdigest()


def _legacy_admission_source_fingerprint(connection: sqlite3.Connection | None = None) -> str:
    """Proof-only extension of the historical generation source fence.

    Keep ordinary generation/checkpoint fingerprints byte-for-byte compatible;
    the explicit admission path needs extra public/Core projection fences.
    """
    def payload(active: sqlite3.Connection) -> list[Any]:
        return [
            _generation_source_fingerprint(active),
            [dict(row) for row in active.execute(
                "SELECT token, nodes_json, runtime_verified_at, updated_at "
                "FROM subscription_profile_snapshots ORDER BY token"
            ).fetchall()],
            [dict(row) for row in active.execute(
                "SELECT id, desired_mode, applied_mode, selective_default, server_mode, "
                "desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
                "active_auto_server_id, apply_state, error_code "
                "FROM routing_global_state ORDER BY id"
            ).fetchall()],
            [dict(row) for row in active.execute(
                "SELECT module_name, desired_state, lifecycle_mode FROM modules ORDER BY module_name"
            ).fetchall()],
            [dict(row) for row in active.execute(
                "SELECT key, value_json FROM settings "
                "WHERE key IN ('routing.auto_selection_provenance', 'vpn_auto_exclusive_source_ref', 'core.bypass', 'core.intent_revision') "
                "ORDER BY key"
            ).fetchall()],
            [dict(row) for row in active.execute(
                "SELECT apply_id, job_id, status, created_at, promoted_at, core_intent_revision "
                "FROM apply_versions ORDER BY apply_id"
            ).fetchall()],
            [dict(row) for row in active.execute(
                "SELECT connection_id, system_id, label, connection_type, runtime_type, "
                "replacement_target, location, address, integration_mode, refresh_mode, enabled, value_json "
                "FROM external_connections ORDER BY connection_id"
            ).fetchall()],
        ]

    if connection is None:
        with db_session() as active:
            values = payload(active)
    else:
        values = payload(connection)
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def _legacy_admission_core_runtime_snapshot(connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Capture Core/module apply observations for one fresh proof attempt only."""
    query = (
        "SELECT module_name, desired_state, lifecycle_mode, runtime_state, apply_state "
        "FROM modules WHERE module_name IN ('core', 'vpn', 'xray', 'watchdog', 'selector') "
        "ORDER BY module_name"
    )
    if connection is None:
        with db_session() as active:
            rows = [dict(row) for row in active.execute(query).fetchall()]
            from fwrouter_api.services.core_intent import read_core_intent_revision
            try:
                revision = read_core_intent_revision(active)
            except ValueError:
                revision = None
            return [*rows, {"core_intent_revision": revision}]
    rows = [dict(row) for row in connection.execute(query).fetchall()]
    from fwrouter_api.services.core_intent import read_core_intent_revision
    try:
        revision = read_core_intent_revision(connection)
    except ValueError:
        revision = None
    return [*rows, {"core_intent_revision": revision}]


def _legacy_admission_core_runtime_fingerprint(connection: sqlite3.Connection | None = None) -> str:
    return hashlib.sha256(json.dumps(
        _legacy_admission_core_runtime_snapshot(connection), sort_keys=True, default=str,
    ).encode()).hexdigest()


def _legacy_admission_core_unavailable_reason(connection: sqlite3.Connection | None = None) -> str | None:
    """Reject bypass, incomplete Core apply state, or an active apply owner."""
    if connection is None:
        with db_session() as active:
            return _legacy_admission_core_unavailable_reason(active)
    bypass_row = connection.execute(
        "SELECT value_json FROM settings WHERE key='core.bypass'",
    ).fetchone()
    if bypass_row is not None:
        try:
            bypass = json.loads(str(bypass_row["value_json"] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return "core_bypass_state_unverifiable"
        if not isinstance(bypass, dict):
            return "core_bypass_state_unverifiable"
        if type(bypass.get("enabled", False)) is not bool:
            return "core_bypass_state_unverifiable"
        if bypass.get("enabled"):
            return "core_bypass_active"

    core = connection.execute(
        "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
    ).fetchone()
    routing = connection.execute(
        "SELECT desired_mode, applied_mode, apply_state FROM routing_global_state WHERE id=1",
    ).fetchone()
    latest_apply = connection.execute(
        "SELECT apply_id, status, core_intent_revision FROM apply_versions ORDER BY created_at DESC, apply_id DESC LIMIT 1",
    ).fetchone()
    if latest_apply is None or latest_apply["status"] != "applied":
        return "core_applied_generation_unowned"
    from fwrouter_api.services.core_intent import read_core_intent_revision
    try:
        current_revision = read_core_intent_revision(connection)
    except ValueError:
        return "core_intent_revision_unverifiable"
    if latest_apply["core_intent_revision"] != current_revision:
        return "core_applied_generation_unowned"
    if (
        core is None or core["desired_state"] != "enabled" or core["lifecycle_mode"] != "managed"
        or core["apply_state"] != "clean" or routing is None
        or routing["apply_state"] != "clean" or routing["desired_mode"] != routing["applied_mode"]
    ):
        return "core_apply_state_unverified"

    # Use the existing lock-key tokenization without its stale-job cleanup side
    # effect. Admission must remain a read-only observer of active owners.
    from fwrouter_api.services.jobs import _lock_tokens
    active_rows = connection.execute(
        "SELECT job_type, lock_key FROM jobs WHERE status IN ('queued', 'running')",
    ).fetchall()
    blocked_tokens = {"apply", "module:core", "selector", "xray"}
    for row in active_rows:
        if row["job_type"] == "core_bypass" or blocked_tokens.intersection(_lock_tokens(row["lock_key"])):
            return "core_apply_inflight"
    return None


def _collect_legacy_admission_core_native_proof() -> dict[str, Any]:
    """Prove the currently-owned Core apply and its live enforcement mode.

    This is a read-only proof collector. It refreshes only existing short-lived
    readback cache keys and never invokes the Core apply owner.
    """
    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status
    from fwrouter_api.services.live_probe_cache import get_live_probe_cache
    from fwrouter_api.services.core_intent import read_core_runtime_context

    try:
        runtime_context_before = read_core_runtime_context()
        if runtime_context_before is None:
            return {"ok": False, "reason": "core_runtime_context_unavailable"}
        manifest = get_live_probe_cache(
            "dataplane_global.applied_manifest", ttl_seconds=5.0,
            loader=dataplane_global._read_applied_manifest_uncached, force_refresh=True,
        )
        if not isinstance(manifest, dict) or not isinstance(manifest.get("plan_id"), str):
            return {"ok": False, "reason": "core_applied_manifest_unavailable"}
        manifest_apply_id = manifest["plan_id"]
        if not manifest_apply_id or (
            "apply_id" in manifest and manifest.get("apply_id") != manifest_apply_id
        ):
            return {"ok": False, "reason": "core_applied_manifest_unavailable"}
        with db_session() as connection:
            routing_row = connection.execute(
                "SELECT desired_mode, applied_mode, selective_default, server_mode, "
                "desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
                "active_auto_server_id, apply_state FROM routing_global_state WHERE id=1",
            ).fetchone()
            owned_apply = connection.execute(
                "SELECT status, core_intent_revision FROM apply_versions WHERE apply_id=?", (manifest_apply_id,),
            ).fetchone()
            latest_apply = connection.execute(
                "SELECT apply_id, status, core_intent_revision FROM apply_versions ORDER BY created_at DESC, apply_id DESC LIMIT 1",
            ).fetchone()
            from fwrouter_api.services.core_intent import read_core_intent_revision
            try:
                core_revision = read_core_intent_revision(connection)
            except ValueError:
                return {"ok": False, "reason": "core_intent_revision_unverifiable"}
            routing = dict(routing_row) if routing_row is not None else None
        if routing is None or routing["apply_state"] != "clean":
            return {"ok": False, "reason": "core_routing_state_unverified"}
        if (
            owned_apply is None or owned_apply["status"] != "applied"
            or latest_apply is None or latest_apply["status"] != "applied"
            or str(latest_apply["apply_id"]) != manifest_apply_id
            or latest_apply["core_intent_revision"] != core_revision
            or owned_apply["core_intent_revision"] != core_revision
        ):
            return {"ok": False, "reason": "core_applied_generation_unowned"}
        manifest_routing = manifest.get("routing_global_state")
        if not isinstance(manifest_routing, dict):
            return {"ok": False, "reason": "core_applied_routing_unavailable"}
        routing_keys = (
            "desired_mode", "applied_mode", "selective_default", "server_mode",
            "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until",
            "active_auto_server_id",
        )
        if any(
            str(manifest_routing.get(key) or "") != str(routing.get(key) or "")
            for key in routing_keys
        ):
            return {"ok": False, "reason": "core_applied_routing_mismatch"}
        mode = str(routing.get("desired_mode") or "").strip().lower()
        if mode not in {"direct", "selective", "vpn"} or routing.get("applied_mode") != mode:
            return {"ok": False, "reason": "core_routing_mode_unverified"}

        payload = dataplane_status._read_live_dataplane_payload()
        if not dataplane_status._live_owned_table_ready(payload):
            return {"ok": False, "reason": "core_live_dataplane_unready"}
        # Refresh the existing active-mode probe explicitly; runtime enforcement
        # consumes this freshly populated cache entry along with the payload.
        get_live_probe_cache(
            "dataplane_live.global_mode", ttl_seconds=2.0,
            loader=dataplane_live._probe_live_global_mode_uncached, force_refresh=True,
        )
        enforcement = dataplane_status.build_runtime_enforcement_state(live_payload=payload)
        expected_levels = {
            "direct": "global_direct_enforced",
            "selective": "global_selective_enforced",
            "vpn": "global_vpn_enforced",
        }
        if (
            not isinstance(enforcement, dict)
            or enforcement.get("enforcement_level") != expected_levels[mode]
            or enforcement.get("traffic_enforcement_guaranteed") is not True
            or enforcement.get("active_mode_matches_intent") is not True
            or str(enforcement.get("live_global_mode") or "").strip().lower() != mode
            or enforcement.get("bypass_active") is True
        ):
            return {"ok": False, "reason": "core_native_enforcement_unproven"}
        manifest_hash = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"),
        ).hexdigest()
        runtime_context_after = read_core_runtime_context()
        if runtime_context_after is None or runtime_context_after != runtime_context_before:
            return {"ok": False, "reason": "core_runtime_context_changed"}
        return {
            "ok": True,
            "apply_id": manifest_apply_id,
            "manifest_hash": manifest_hash,
            "enforcement_level": enforcement["enforcement_level"],
            "traffic_enforcement_guaranteed": True,
            "active_mode_matches_intent": True,
            "live_global_mode": mode,
            "live_selective_default": enforcement.get("live_selective_default"),
            "runtime_context": runtime_context_after,
        }
    except Exception:
        return {"ok": False, "reason": "core_native_readback_unavailable"}


def _legacy_admission_checkpoint_parent_ready(path: Path) -> bool:
    """Create a new private checkpoint parent safely, without following or chmodding it."""
    parent = path.parent
    try:
        try:
            before = parent.lstat()
        except FileNotFoundError:
            ancestor = parent.parent.lstat()
            if not stat.S_ISDIR(ancestor.st_mode) or stat.S_ISLNK(ancestor.st_mode) or ancestor.st_uid != os.geteuid():
                return False
            try:
                os.mkdir(parent, 0o700)
            except FileExistsError:
                pass
            before = parent.lstat()
        if (
            not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077
            or stat.S_IMODE(before.st_mode) & 0o700 != 0o700
        ):
            return False
        descriptor = os.open(
            parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = parent.lstat()
        return bool(
            stat.S_ISDIR(opened.st_mode) and opened.st_uid == os.geteuid()
            and stat.S_IMODE(opened.st_mode) & 0o077 == 0
            and (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino)
            and (after.st_dev, after.st_ino) == (opened.st_dev, opened.st_ino)
        )
    except OSError:
        return False


def _legacy_mihomo_candidate_matches(active_path: Path, candidate_path: Path) -> bool:
    """Compare a private admission candidate with active Mihomo without applying it.

    The builder records which previous FWRouter inbounds it removed from its
    active-config base. These fields describe sanitization provenance rather
    than runtime policy. Accept their change only when the candidate exactly
    records a fresh sanitization of the active config; compare every other
    field strictly.
    """
    def digest(path: Path) -> bytes | None:
        hasher = hashlib.sha256()
        try:
            before_path = path.lstat()
            if not stat.S_ISREG(before_path.st_mode) or stat.S_ISLNK(before_path.st_mode):
                return None
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino)
                ):
                    return None
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            after_path = path.lstat()
            def stable_fields(value: os.stat_result) -> tuple[int, int, int, int, int]:
                return (
                    value.st_dev, value.st_ino, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns,
                )
            if (
                not stat.S_ISREG(after_path.st_mode) or stat.S_ISLNK(after_path.st_mode)
                or stable_fields(before) != stable_fields(after)
                or (after.st_dev, after.st_ino) != (after_path.st_dev, after_path.st_ino)
            ):
                return None
            return hasher.digest()
        except OSError:
            return None
    try:
        active_digest = digest(active_path)
        candidate_digest = digest(candidate_path)
        if active_digest is None or candidate_digest is None:
            return False
        if active_digest == candidate_digest:
            return True
    except (OSError, ValueError):
        return False

    import copy
    import yaml

    from fwrouter_api.services.mihomo_config_inbounds import _sanitize_fwrouter_managed_inbounds
    from fwrouter_api.services.mihomo_config_paths import MAX_BASE_CONFIG_BYTES
    from fwrouter_api.services.mihomo_config_status import _configs_equal
    from fwrouter_api.services.mihomo_reconcile import (
        MAX_SEMANTIC_CONFIG_BYTES, _yaml_tree_is_unambiguous, _yaml_value_is_json_safe,
    )

    max_bytes = min(MAX_BASE_CONFIG_BYTES, MAX_SEMANTIC_CONFIG_BYTES)

    def load_bounded(path: Path) -> dict[str, Any] | None:
        try:
            before_path = path.lstat()
            if not stat.S_ISREG(before_path.st_mode) or stat.S_ISLNK(before_path.st_mode):
                return None
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                    return None
                chunks: list[bytes] = []
                total = 0
                while total <= max_bytes:
                    chunk = os.read(descriptor, min(65536, max_bytes + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                raw = b"".join(chunks)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            after_path = path.lstat()
            if (
                len(raw) > max_bytes or len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (before.st_dev, before.st_ino)
                != (before_path.st_dev, before_path.st_ino)
                or (after.st_dev, after.st_ino)
                != (after_path.st_dev, after_path.st_ino)
                or stat.S_ISLNK(after_path.st_mode)
            ):
                return None
            text = raw.decode("utf-8")
            loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
            tree = yaml.compose(text, Loader=loader)
            if not _yaml_tree_is_unambiguous(tree):
                return None
            value = yaml.load(text, Loader=loader)
            if not isinstance(value, dict) or not _yaml_value_is_json_safe(value):
                return None
            return value
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
            return None

    active = load_bounded(active_path)
    candidate = load_bounded(candidate_path)
    if active is None or candidate is None:
        return False
    active_meta = active.get("fwrouter")
    candidate_meta = candidate.get("fwrouter")
    if not isinstance(active_meta, dict) or not isinstance(candidate_meta, dict):
        return False

    # Existing active files may predate these provenance fields. If present,
    # require bounded string lists; never trust arbitrary YAML types here.
    provenance_fields = (
        "sanitized_legacy_inbound_keys", "sanitized_managed_listeners",
    )
    for key in provenance_fields:
        candidate_value = candidate_meta.get(key)
        if key not in candidate_meta or not isinstance(candidate_value, list) or len(candidate_value) > 4096:
            return False
        if any(not isinstance(item, str) or len(item) > 512 for item in candidate_value):
            return False
        if key in active_meta:
            active_value = active_meta[key]
            if not isinstance(active_value, list) or len(active_value) > 4096:
                return False
            if any(not isinstance(item, str) or len(item) > 512 for item in active_value):
                return False

    # Derive expected provenance from the exact active config. This is not a
    # blanket fwrouter-section exemption: wrong or invented removal records
    # still reject, and all policy/runtime fields remain part of the comparison.
    try:
        _, sanitization = _sanitize_fwrouter_managed_inbounds(copy.deepcopy(active))
    except (TypeError, ValueError):
        return False
    provenance = {
        "sanitized_legacy_inbound_keys": sanitization.get("removed_top_level_keys"),
        "sanitized_managed_listeners": sanitization.get("removed_listener_names"),
    }
    # Legacy top-level inbound ports are live listeners in the active config.
    # Removing them changes runtime state, so their presence cannot be treated
    # as a provenance-only difference during proof admission.
    if provenance["sanitized_legacy_inbound_keys"]:
        return False
    if any(
        not isinstance(expected, list)
        or candidate_meta.get(key) != expected
        for key, expected in provenance.items()
    ):
        return False

    normalized_active = copy.deepcopy(active)
    normalized_meta = normalized_active.get("fwrouter")
    if not isinstance(normalized_meta, dict):
        return False
    normalized_meta.update(provenance)
    return _configs_equal(normalized_active, candidate)


_PROOF_ONLY_LEGACY_ADMISSION_CONFIRMATION = "PROOF_ONLY_LEGACY_ADMISSION"


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
        "schema", "operation_mode", "operation_id", "phase", "candidate_digest",
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


def _legacy_admission_runtime_fence_facts(admission: Any) -> dict[str, bool]:
    """Return closed-vocabulary runtime fence facts from one fresh readback pass."""
    facts = {
        "xray_incarnation_match": False,
        "xray_runtime_config_match": False,
        "xray_active_config_match": False,
        "mihomo_incarnation_match": False,
        "mihomo_active_config_match": False,
        "mihomo_input_fingerprint_match": False,
        "mihomo_routing_snapshot_match": False,
        "core_runtime_fingerprint_match": False,
        "core_state_available": False,
        "core_native_proof_ok": False,
        "core_manifest_match": False,
        "core_apply_id_match": False,
        "core_enforcement_match": False,
        "core_live_mode_match": False,
    }
    try:
        adapter = _xray_adapter()
        from fwrouter_api.services import mihomo_config
        from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint
        from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
        with db_session() as connection:
            core_fingerprint = _legacy_admission_core_runtime_fingerprint(connection)
            core_unavailable = _legacy_admission_core_unavailable_reason(connection)
        xray_sha = hashlib.sha256(Path(adapter.config_path).read_bytes()).hexdigest()
        mihomo_path = Path(mihomo_config._resolved_base_config_path())
        mihomo_sha = hashlib.sha256(mihomo_path.read_bytes()).hexdigest()
        selection = _capture_generation_auto_selection()
        expected_selection = getattr(admission, "proof_only_selection_snapshot", None)
        routing_snapshot = getattr(admission, "proof_only_routing_snapshot", None)
        # Keep the historical no-argument path for ordinary callers/adapters;
        # only an explicitly captured fixed projection is passed positionally.
        fingerprint = current_mihomo_input_fingerprint(routing_snapshot) if isinstance(routing_snapshot, dict) else current_mihomo_input_fingerprint()
        core_native = _collect_legacy_admission_core_native_proof()
        xray_incarnation_reader = getattr(adapter, "get_runtime_incarnation", None)
        xray_config_reader = getattr(adapter, "get_runtime_config_sha256", None)
        facts.update({
            "xray_incarnation_match": bool(
                callable(xray_incarnation_reader)
                and xray_incarnation_reader() == admission.proof_only_xray_incarnation
            ),
            "xray_runtime_config_match": bool(
                callable(xray_config_reader)
                and xray_config_reader() == admission.proof_only_xray_config_sha256
            ),
            "xray_active_config_match": (
                admission.proof_only_xray_config_sha256 == xray_sha
            ),
            "mihomo_incarnation_match": (
                get_mihomo_runtime_incarnation() == admission.proof_only_mihomo_incarnation
            ),
            "mihomo_active_config_match": (
                mihomo_sha == admission.proof_only_mihomo_config_sha256
            ),
            "mihomo_input_fingerprint_match": (
                fingerprint.get("hash") == admission.proof_only_mihomo_fingerprint_hash
            ),
            "mihomo_routing_snapshot_match": (
                isinstance(expected_selection, dict)
                and selection == expected_selection
                and isinstance(routing_snapshot, dict)
                and selection.get("routing") == routing_snapshot
            ),
            "core_runtime_fingerprint_match": (
                core_fingerprint == admission.proof_only_core_runtime_fingerprint
            ),
            "core_state_available": core_unavailable is None,
            "core_native_proof_ok": core_native.get("ok") is True,
            "core_manifest_match": (
                core_native.get("manifest_hash") == admission.proof_only_core_manifest_hash
            ),
            "core_apply_id_match": (
                core_native.get("apply_id") == admission.proof_only_core_apply_id
            ),
            "core_enforcement_match": (
                core_native.get("enforcement_level") == admission.proof_only_core_enforcement_level
            ),
            "core_live_mode_match": (
                core_native.get("live_global_mode") == admission.proof_only_core_live_mode
            ),
        })
    except Exception:
        return facts
    return facts


def _legacy_admission_runtime_fences_match(admission: Any) -> bool:
    """Check live native incarnation/config fences immediately before promotion."""
    required = {
        "xray_incarnation_match", "xray_runtime_config_match", "xray_active_config_match",
        "mihomo_incarnation_match", "mihomo_active_config_match", "mihomo_input_fingerprint_match",
        "mihomo_routing_snapshot_match",
        "core_runtime_fingerprint_match", "core_state_available", "core_native_proof_ok",
        "core_manifest_match", "core_apply_id_match", "core_enforcement_match", "core_live_mode_match",
    }
    facts = _legacy_admission_runtime_fence_facts(admission)
    return set(facts) == required and all(facts.get(key) is True for key in required)


def _legacy_admission_durable_fences_match(
    *, expected_digest: str, expected_source: str, selection_revision: int,
    selection_decision_id: str | None, expected_status: str, expected_revision: int,
    expected_core_runtime_fingerprint: str,
) -> bool:
    """Recheck the admission tuple in one immediate transaction before closure."""
    try:
        from fwrouter_api.services.subscription import _read_subscription_native_generation, _subscription_inventory_digest
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        with db_session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            digest, count = _subscription_inventory_digest(connection)
            source = _legacy_admission_source_fingerprint(connection)
            selection = read_selection_fence(connection)
            generation = _read_subscription_native_generation(connection)
            return bool(
                digest == expected_digest and count > 0 and source == expected_source
                and selection.get("revision") == selection_revision
                and selection.get("decision_id") == selection_decision_id
                and _legacy_admission_core_runtime_fingerprint(connection)
                == expected_core_runtime_fingerprint
                and _legacy_admission_core_unavailable_reason(connection) is None
                and generation.get("status") == expected_status
                and generation.get("candidate_digest") == expected_digest
                and generation.get("candidate_revision") == expected_revision
                and (expected_status != "verified" or (
                    generation.get("verified_digest") == expected_digest
                    and generation.get("verified_revision") == expected_revision
                ))
            )
    except Exception:
        return False


def _collect_legacy_inventory_native_proof(
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
        active_mihomo_sha = hashlib.sha256(mihomo_path.read_bytes()).hexdigest()
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
            or hashlib.sha256(mihomo_path.read_bytes()).hexdigest() != active_mihomo_sha
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


def _publish_legacy_admission_mihomo_receipt(proof: dict[str, Any], operation_id: str) -> bool:
    if not isinstance(proof, dict) or proof.get("mihomo_native_policy_match") is not True:
        return False
    try:
        fence_view = _AdmissionFenceView(
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
    except (KeyError, TypeError, ValueError):
        return False
    return _publish_mihomo_udp_receipt_with_owner(
        expected_mihomo_runtime=str(proof.get("mihomo_runtime_incarnation") or ""),
        expected_mihomo_sha=str(proof.get("mihomo_config_sha256") or ""),
        reconcile_reason="proof_only_legacy_admission",
        operation_id=operation_id,
        routing_snapshot=proof.get("routing_snapshot"),
        owner_matches=lambda fingerprint_hash: (
            fingerprint_hash == proof.get("mihomo_fingerprint_hash")
            and _legacy_admission_source_fingerprint() == proof.get("source_fingerprint")
            and _legacy_admission_runtime_fences_match(fence_view)
        ),
    )


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


def admit_legacy_subscription_inventory(
    *, expected_digest: str, operator_confirmation: str,
) -> dict[str, Any]:
    """Explicitly admit an unmarked legacy inventory after fresh native proof.

    Generic startup/recovery does not invoke this operation. It is intentionally
    operator-confirmed, exact-digest-bound, and unable to mutate providers,
    credentials, or native runtime. Failures retain the private owner checkpoint
    so only this same explicit operation can retry.
    """
    import re
    if operator_confirmation != _PROOF_ONLY_LEGACY_ADMISSION_CONFIRMATION:
        return {"ok": False, "status": "blocked", "reason": "operator_confirmation_required"}
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        return {"ok": False, "status": "blocked", "reason": "inventory_digest_invalid"}
    try:
        with xray_writer_guard(timeout_seconds=30.0):
            adapter = _xray_adapter()
            checkpoint_path = _xray_generation_checkpoint_path(adapter)
            from fwrouter_api.services.subscription import (
                SUBSCRIPTION_NATIVE_GENERATION_KEY, _read_subscription_native_generation,
                _subscription_inventory_digest, _write_subscription_inventory_candidate,
                _claim_subscription_inventory_admission, _promote_subscription_inventory_generation,
            )
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
            with db_session() as connection:
                from fwrouter_api.services.xray_vpn_auto_pending import _admission_fixed_workflow_pending
                if _admission_fixed_workflow_pending(connection):
                    return {"ok": False, "status": "blocked", "reason": "fixed_server_workflow_pending"}
                digest, count = _subscription_inventory_digest(connection)
                state = _read_subscription_native_generation(connection)
                marker_row = connection.execute(
                    "SELECT value_json FROM settings WHERE key=?", (SUBSCRIPTION_NATIVE_GENERATION_KEY,),
                ).fetchone()
                source_fingerprint = _legacy_admission_source_fingerprint(connection)
                selection = read_selection_fence(connection)
                core_unavailable = _legacy_admission_core_unavailable_reason(connection)
                if digest != expected_digest or count <= 0:
                    return {"ok": False, "status": "blocked", "reason": "inventory_digest_mismatch"}
                if core_unavailable is not None:
                    return {"ok": False, "status": "blocked", "reason": core_unavailable}

            checkpoint_entry_exists = checkpoint_path.exists() or checkpoint_path.is_symlink()
            checkpoint = _legacy_admission_checkpoint_payload(checkpoint_path) if checkpoint_entry_exists else None
            if checkpoint_entry_exists and checkpoint is None:
                return {"ok": False, "status": "blocked", "reason": "checkpoint_unverifiable"}
            if checkpoint is not None:
                if checkpoint.get("candidate_digest") != expected_digest:
                    return {"ok": False, "status": "blocked", "reason": "checkpoint_inventory_mismatch"}
                if checkpoint.get("source_fingerprint") != source_fingerprint:
                    return {"ok": False, "status": "blocked", "reason": "legacy_admission_fence_changed"}
                if checkpoint.get("selection_revision") != selection.get("revision") or checkpoint.get(
                    "selection_decision_id"
                ) != selection.get("decision_id"):
                    return {"ok": False, "status": "blocked", "reason": "selection_fence_changed"}
                expected_revision = checkpoint["candidate_revision"]
                if state.get("status") == "pending" and (
                    state.get("candidate_digest") != expected_digest
                    or state.get("candidate_revision") != expected_revision
                ):
                    return {"ok": False, "status": "blocked", "reason": "inventory_generation_stale"}
                if state.get("status") == "verified" and (
                    state.get("candidate_digest") != expected_digest
                    or state.get("candidate_revision") != expected_revision
                    or state.get("verified_digest") != expected_digest
                    or state.get("verified_revision") != expected_revision
                ):
                    return {"ok": False, "status": "blocked", "reason": "inventory_generation_stale"}
                if state.get("status") not in {"unknown", "pending", "verified"}:
                    return {"ok": False, "status": "blocked", "reason": "inventory_generation_unexpected"}
            else:
                if marker_row is not None:
                    # Pending/unknown markers and malformed state are owned by
                    # their existing lifecycle; admission cannot appropriate it.
                    # A verified exact marker may be re-proved idempotently.
                    if not (
                        state.get("status") == "verified"
                        and state.get("candidate_digest") == expected_digest
                        and state.get("verified_digest") == expected_digest
                        and type(state.get("candidate_revision")) is int
                        and state.get("candidate_revision") == state.get("verified_revision")
                    ):
                        return {"ok": False, "status": "blocked", "reason": "generation_marker_already_present"}
                    candidate_revision = int(state["candidate_revision"])
                else:
                    if state.get("status") != "unknown" or state.get("reason") != "verified_generation_missing":
                        return {"ok": False, "status": "blocked", "reason": "legacy_inventory_not_admissible"}
                    candidate_revision = 1
                checkpoint = {
                    "schema": 1,
                    "operation_mode": "proof_only_legacy_admission",
                    "operation_id": uuid4().hex,
                    "phase": "proof_started",
                    "candidate_digest": expected_digest,
                    "candidate_revision": candidate_revision,
                    "source_fingerprint": source_fingerprint,
                    "selection_revision": selection.get("revision"),
                    "selection_decision_id": selection.get("decision_id"),
                    "created_at": time.time(),
                }
                if not _legacy_admission_checkpoint_parent_ready(checkpoint_path):
                    return {"ok": False, "status": "blocked", "reason": "checkpoint_directory_untrusted"}
                _atomic_write_private_checkpoint(
                    checkpoint_path, json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),
                )
            assert checkpoint is not None
            required_truths = (
                "xray_config_match", "xray_loaded_users_match", "xray_bindings_match",
                "xray_modes_match", "core_selection_match", "mihomo_candidate_match",
                "mihomo_native_policy_match", "core_native_policy_match",
            )

            def collect_current_proof() -> tuple[dict[str, Any] | None, str | None]:
                current_proof = _collect_legacy_inventory_native_proof(
                    adapter, expected_digest=expected_digest,
                    source_fingerprint=source_fingerprint, checkpoint_path=checkpoint_path,
                )
                if (current_proof.get("ok") is not True
                        or any(current_proof.get(key) is not True for key in required_truths)):
                    reason = current_proof.get("reason")
                    return None, reason if isinstance(reason, str) else "native_proof_incomplete"
                if current_proof.get("source_fingerprint") != source_fingerprint:
                    return None, "legacy_admission_fence_changed"
                current_selection = _capture_generation_auto_selection()
                if (current_selection.get("selection_revision") != checkpoint.get("selection_revision")
                        or current_selection.get("selection_decision_id") != checkpoint.get("selection_decision_id")):
                    return None, "selection_fence_changed"
                with db_session() as connection:
                    current_inventory_digest, current_inventory_count = _subscription_inventory_digest(connection)
                    current_source_fingerprint = _legacy_admission_source_fingerprint(connection)
                    core_unavailable = _legacy_admission_core_unavailable_reason(connection)
                    current_core_runtime_fingerprint = _legacy_admission_core_runtime_fingerprint(connection)
                    current_generation = _read_subscription_native_generation(connection)
                if (current_inventory_digest != expected_digest or current_inventory_count <= 0
                        or current_source_fingerprint != source_fingerprint or core_unavailable is not None
                        or current_core_runtime_fingerprint != current_proof.get("core_runtime_fingerprint")):
                    return None, "legacy_admission_fence_changed"
                generation_status = current_generation.get("status")
                if generation_status == "unknown":
                    if (current_generation.get("reason") != "verified_generation_missing"
                            or current_generation.get("candidate_revision") is not None
                            or checkpoint.get("candidate_revision") != 1):
                        return None, "inventory_generation_stale"
                elif generation_status in {"pending", "verified"}:
                    if (current_generation.get("candidate_digest") != expected_digest
                            or current_generation.get("candidate_revision")
                            != checkpoint.get("candidate_revision")):
                        return None, "inventory_generation_stale"
                else:
                    return None, "inventory_generation_stale"
                return current_proof, None

            def record_proof_complete(current_proof: dict[str, Any]) -> None:
                # This phase means a fresh native proof was collected. It does
                # not mean that a receipt or verified generation was published.
                checkpoint.update({
                    "phase": "proof_complete",
                    "proof_summary": {key: True for key in required_truths},
                    "xray_runtime_incarnation": current_proof["xray_runtime_incarnation"],
                    "mihomo_runtime_incarnation": current_proof["mihomo_runtime_incarnation"],
                    "xray_config_sha256": current_proof["xray_config_sha256"],
                    "mihomo_config_sha256": current_proof["mihomo_config_sha256"],
                    "mihomo_fingerprint_hash": current_proof["mihomo_fingerprint_hash"],
                })
                _atomic_write_private_checkpoint(
                    checkpoint_path, json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),
                )

            proof, proof_error = collect_current_proof()
            if proof is None:
                return {"ok": False, "status": "blocked", "reason": proof_error or "native_proof_incomplete",
                        "last_good_retained": True}
            # Keep this checkpoint resumable across the marker-write crash
            # window. A resume always gathers fresh proof before receipt/promotion.
            record_proof_complete(proof)

            # Close the missing-marker crash window by publishing a pending
            # generation only after proof, under the same immediate SQLite
            # transaction that verifies every durable source and selection
            # fence. Existing verified exact-generation retries do not bump it.
            if state.get("status") == "verified":
                if not _publish_legacy_admission_mihomo_receipt(proof, str(checkpoint["operation_id"])):
                    return {"ok": False, "status": "blocked", "reason": "native_receipt_unverified",
                            "last_good_retained": True}
                already_admitted = _AdmissionFenceView(
                    proof_only_source_fingerprint=source_fingerprint,
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
                if (
                    not _legacy_admission_runtime_fences_match(already_admitted)
                    or not _legacy_admission_durable_fences_match(
                        expected_digest=expected_digest, expected_source=source_fingerprint,
                        selection_revision=int(proof["selection_revision"]),
                        selection_decision_id=proof.get("selection_decision_id"),
                        expected_status="verified", expected_revision=int(state["candidate_revision"]),
                        expected_core_runtime_fingerprint=str(proof["core_runtime_fingerprint"]),
                    )
                ):
                    return {"ok": False, "status": "blocked", "reason": "legacy_admission_fence_changed",
                            "last_good_retained": True}
                checkpoint["phase"] = "verified"
                _atomic_write_private_checkpoint(
                    checkpoint_path, json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),
                )
                checkpoint_path.unlink(missing_ok=True)
                _fsync_directory(checkpoint_path.parent)
                return {"ok": True, "status": "verified", "already_admitted": True,
                        "digest": expected_digest, "revision": state["candidate_revision"],
                        "proof_receipt_written": True}
            if state.get("status") == "unknown":
                with db_session() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current_digest, current_count = _subscription_inventory_digest(connection)
                    current_marker = connection.execute(
                        "SELECT 1 FROM settings WHERE key=?", (SUBSCRIPTION_NATIVE_GENERATION_KEY,),
                    ).fetchone()
                    current_selection = read_selection_fence(connection)
                    if (
                        current_marker is not None or current_digest != expected_digest or current_count <= 0
                        or _legacy_admission_source_fingerprint(connection) != source_fingerprint
                        or current_selection.get("revision") != selection.get("revision")
                        or current_selection.get("decision_id") != selection.get("decision_id")
                    ):
                        return {"ok": False, "status": "blocked", "reason": "legacy_admission_fence_changed",
                                "last_good_retained": True}
                    _write_subscription_inventory_candidate(connection, force_pending=True)
                    current = _read_subscription_native_generation(connection)
                    if current.get("status") != "pending" or current.get("candidate_digest") != expected_digest:
                        return {"ok": False, "status": "blocked", "reason": "pending_generation_publish_failed",
                                "last_good_retained": True}
                    if current.get("candidate_revision") != checkpoint.get("candidate_revision"):
                        checkpoint["candidate_revision"] = current.get("candidate_revision")
                        _atomic_write_private_checkpoint(
                            checkpoint_path, json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),
                        )
                    # Subsequent capability/receipt work belongs to the
                    # pending generation, not the stale pre-marker snapshot.
                    state = current
                # Publishing the generation marker changes Mihomo's input
                # fingerprint. Re-prove that exact pending generation before
                # writing a receipt, so promotion consumes the same fence.
                proof, proof_error = collect_current_proof()
                if proof is None:
                    return {"ok": False, "status": "blocked", "reason": proof_error or "native_proof_incomplete",
                            "last_good_retained": True}
                record_proof_complete(proof)
            admission = _claim_subscription_inventory_admission()
            if admission is None:
                return {"ok": False, "status": "blocked", "reason": "inventory_generation_admission_missing",
                        "last_good_retained": True}
            from dataclasses import replace
            admission = replace(
                admission,
                proof_only_source_fingerprint=source_fingerprint,
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
            if not _publish_legacy_admission_mihomo_receipt(proof, str(checkpoint["operation_id"])):
                return {"ok": False, "status": "blocked", "reason": "native_receipt_unverified",
                        "last_good_retained": True}
            promoted = _promote_subscription_inventory_generation(admission)
            if not promoted.get("ok"):
                return {"ok": False, "status": "blocked", "reason": promoted.get("reason") or "inventory_promotion_failed",
                        "last_good_retained": True}
            checkpoint["phase"] = "verified"
            _atomic_write_private_checkpoint(
                checkpoint_path, json.dumps(checkpoint, sort_keys=True, separators=(",", ":")),
            )
            checkpoint_path.unlink(missing_ok=True)
            _fsync_directory(checkpoint_path.parent)
            return {"ok": True, "status": "verified", "digest": expected_digest,
                    "revision": promoted.get("revision", admission.revision), "already_admitted": False,
                    "proof_receipt_written": True}
    except Exception as exc:
        name = type(exc).__name__
        safe_name = name if name.isascii() and name.isalnum() and name.endswith(("Error", "Exception")) else "admission_error"
        return {"ok": False, "status": "blocked", "reason": safe_name,
                "last_good_retained": True}


def inspect_legacy_subscription_inventory_admission() -> dict[str, Any]:
    """Return a read-only operator plan without native reads or state writes."""
    try:
        from fwrouter_api.db.connection import get_db_path
        from fwrouter_api.services.subscription import (
            SUBSCRIPTION_NATIVE_GENERATION_KEY, _read_subscription_native_generation,
            _subscription_inventory_digest,
        )
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        from fwrouter_api.adapters.xray_common import _default_xray_config_path
        checkpoint_path = Path(_default_xray_config_path()).parent / ".generation" / "generation-checkpoint.json"
        db_path = get_db_path().resolve(strict=True)
        readonly_uri = db_path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(readonly_uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            digest, count = _subscription_inventory_digest(connection)
            generation = _read_subscription_native_generation(connection)
            marker = connection.execute(
                "SELECT 1 FROM settings WHERE key=?", (SUBSCRIPTION_NATIVE_GENERATION_KEY,),
            ).fetchone()
            source_fingerprint = _legacy_admission_source_fingerprint(connection)
            selection = read_selection_fence(connection)
            core_unavailable = _legacy_admission_core_unavailable_reason(connection)
        finally:
            connection.close()
        checkpoint_entry_exists = checkpoint_path.exists() or checkpoint_path.is_symlink()
        checkpoint = _legacy_admission_checkpoint_payload(checkpoint_path) if checkpoint_entry_exists else None
        if checkpoint_entry_exists and checkpoint is None:
            checkpoint_mode, checkpoint_phase = "unknown", "unknown"
        else:
            checkpoint_mode = checkpoint.get("operation_mode") if checkpoint else None
            checkpoint_phase = checkpoint.get("phase") if checkpoint else None
        action, reason = "blocked", None
        if not count or digest != generation.get("inventory_digest"):
            reason = "inventory_digest_unavailable_or_mismatched"
        elif core_unavailable is not None:
            reason = core_unavailable
        elif checkpoint_entry_exists and checkpoint is None:
            reason = "checkpoint_unverifiable"
        elif checkpoint is not None:
            if checkpoint.get("candidate_digest") != digest:
                reason = "checkpoint_inventory_mismatch"
            elif checkpoint.get("source_fingerprint") != source_fingerprint:
                reason = "legacy_admission_fence_changed"
            elif (checkpoint.get("selection_revision") != selection.get("revision")
                  or checkpoint.get("selection_decision_id") != selection.get("decision_id")):
                reason = "selection_fence_changed"
            elif generation.get("status") == "unknown" and marker is None:
                if (checkpoint.get("candidate_revision") == 1
                        and generation.get("reason") == "verified_generation_missing"
                        and checkpoint.get("phase") != "verified"):
                    action = "resume_proof_only"
                else:
                    reason = "checkpoint_generation_state_mismatch"
            elif generation.get("status") == "pending" and (
                generation.get("candidate_digest") == digest
                and generation.get("candidate_revision") == checkpoint.get("candidate_revision")
                and checkpoint.get("phase") in {"proof_complete", "verified"}
            ):
                action = "resume_proof_only"
            elif generation.get("status") == "verified" and (
                generation.get("candidate_digest") == digest
                and generation.get("verified_digest") == digest
                and generation.get("candidate_revision") == checkpoint.get("candidate_revision")
                and generation.get("verified_revision") == checkpoint.get("candidate_revision")
            ):
                action = "reprove_verified"
            else:
                reason = "checkpoint_generation_state_mismatch"
        elif marker is None and generation.get("status") == "unknown" and (
            generation.get("reason") == "verified_generation_missing"
        ):
            action = "fresh_admission"
        elif marker is not None and generation.get("status") == "verified" and (
            generation.get("candidate_digest") == digest
            and generation.get("verified_digest") == digest
            and type(generation.get("candidate_revision")) is int
            and generation.get("candidate_revision") == generation.get("verified_revision")
        ):
            action = "reprove_verified"
        else:
            reason = "generation_marker_not_admissible"
        eligible = action != "blocked"
        return {
            "ok": True, "status": "plan", "eligible": eligible,
            "action": action, "reason": reason, "native_proof_required": True,
            "inventory_digest": digest, "inventory_count": count,
            "generation_status": generation.get("status"),
            "generation_reason": generation.get("reason"),
            "checkpoint_mode": checkpoint_mode, "checkpoint_phase": checkpoint_phase,
            "source_fingerprint": source_fingerprint,
        }
    except Exception as exc:
        name = type(exc).__name__
        safe_name = name if name.isascii() and name.isalnum() and name.endswith(("Error", "Exception")) else "inspection_error"
        return {"ok": False, "status": "unavailable", "reason": safe_name}


def _current_inventory_generation_binding() -> dict[str, Any]:
    from fwrouter_api.services.subscription import get_subscription_native_generation_state
    state = get_subscription_native_generation_state()
    if state.get("status") == "empty":
        return {"digest": None, "revision": None}
    return {"digest": state.get("candidate_digest"), "revision": state.get("candidate_revision")}


def _checkpoint_inventory_generation_matches(checkpoint_path: Path) -> bool:
    """A current-projection recovery must be bound to this exact inventory."""
    try:
        loaded_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint = loaded_checkpoint if isinstance(loaded_checkpoint, dict) else {}
        binding = checkpoint.get("subscription_inventory_generation")
        from fwrouter_api.services.subscription import get_subscription_native_generation_state
        state = get_subscription_native_generation_state()
    except Exception:
        return False
    if not isinstance(binding, dict):
        return False
    if state.get("status") == "empty":
        return binding.get("digest") is None and binding.get("revision") is None
    return bool(
        state.get("status") in {"pending", "verified"}
        and binding.get("digest") == state.get("candidate_digest")
        and type(binding.get("revision")) is int
        and binding.get("revision") == state.get("candidate_revision")
    )


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


def _resolve_existing_xray_generation_checkpoint(adapter: Any, checkpoint_path: Path) -> dict[str, Any]:
    """Resolve an existing generation marker using the normal proof contracts.

    Caller owns the Xray writer guard. This is shared by normal reconcile and
    the refresh preflight so provider inventory cannot supersede an unresolved
    checkpoint before its bound generation is recovered.
    """
    if not checkpoint_path.exists():
        return {"ok": True, "status": "skipped", "reason": "no_checkpoint"}
    try:
        from fwrouter_api.services.server_state import get_routing_global_state

        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        mode = _checkpoint_operation_mode(checkpoint)
        if mode == "proof_only_legacy_admission":
            return {"ok": False, "status": "pending", "stage": "proof_only_legacy_admission",
                    "reason": "proof_only_operation_requires_explicit_resume",
                    "error_code": "XRAY_PROOF_ONLY_ADMISSION_REQUIRES_OPERATOR"}
        checkpoint_phase = str(checkpoint.get("phase") or "") if isinstance(checkpoint, dict) else "invalid"
        with db_session() as connection:
            from fwrouter_api.services.xray_vpn_auto_pending import _load_row
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
            pending_fixed = _load_row(connection)
            pending_workflow = pending_fixed.get("workflow") if isinstance(pending_fixed, dict) else None
            current_selection_revision = read_selection_revision(connection)
        fixed_operation = checkpoint.get("fixed_workflow_revision")
        if isinstance(pending_workflow, dict) and pending_workflow.get("kind") == "global_fixed_server":
            fixed_snapshot = checkpoint.get("fixed_routing_snapshot")
            fixed_origin_revision = checkpoint.get("fixed_origin_selection_revision")
            checkpoint_selection_revision = checkpoint.get("selection_revision")
            receipt_owned = bool(
                isinstance(pending_workflow, dict)
                and type(checkpoint_selection_revision) is int
                and pending_workflow.get("selection_revision") == checkpoint_selection_revision
                and pending_workflow.get("xray_generation_operation_id")
                    == checkpoint.get("selection_operation_id")
            )
            origin_owned = bool(
                type(fixed_origin_revision) is int
                and pending_workflow.get("selection_revision") == fixed_origin_revision
            )
            if (
                type(fixed_operation) is not int
                or type(fixed_origin_revision) is not int
                or type(checkpoint_selection_revision) is not int
                or checkpoint_selection_revision < fixed_origin_revision
                or not (origin_owned or receipt_owned)
                or pending_fixed.get("revision") != fixed_operation
                or current_selection_revision != checkpoint_selection_revision
                or not isinstance(fixed_snapshot, dict)
                or not _fixed_xray_routing_projection(get_routing_global_state(expire_ttl=False)) == fixed_snapshot
            ):
                return {"ok": False, "status": "pending", "stage": "fixed_generation_recovery_fence",
                        "error_code": "FIXED_XRAY_GENERATION_RECOVERY_UNPROVEN",
                        "runtime_verified": False}
            # Fixed generation rollback has no qualified preimage/native proof
            # contract yet. Keep its immutable checkpoint and pending intent.
            return {"ok": False, "status": "pending", "stage": "fixed_generation_recovery_fence",
                    "error_code": "FIXED_XRAY_GENERATION_RECOVERY_UNPROVEN",
                    "runtime_verified": False}
        elif fixed_operation is not None:
            return {"ok": False, "status": "pending", "stage": "fixed_generation_recovery_fence",
                    "error_code": "FIXED_XRAY_GENERATION_RECOVERY_UNPROVEN",
                    "runtime_verified": False}
        elif isinstance(checkpoint.get("fixed_routing_snapshot"), dict):
            return {"ok": False, "status": "pending", "stage": "fixed_generation_recovery_fence",
                    "error_code": "FIXED_XRAY_GENERATION_RECOVERY_UNPROVEN",
                    "runtime_verified": False}
        checkpoint_inventory_bound = _checkpoint_inventory_generation_matches(checkpoint_path)
        from fwrouter_api.services.subscription import get_subscription_native_generation_state
        inventory_state = get_subscription_native_generation_state()
        checkpoint_digest = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
        with db_session() as connection:
            migration_tied_to_checkpoint = connection.execute(
                "SELECT 1 FROM subscription_node_identity_transitions "
                "WHERE checkpoint_sha256=? LIMIT 1", (checkpoint_digest,),
            ).fetchone()
        if migration_tied_to_checkpoint:
            return {"ok": False, "status": "pending", "stage": "generation_supersession_required",
                    "error_code": "XRAY_GENERATION_SUPERSESSION_REQUIRED", "last_good_retained": True}
        current_projection_recoverable = checkpoint_inventory_bound and (
            checkpoint_phase in {
                "runtime_applied", "inventory_synced", "bindings_written",
                "projections_cleaned", "selection_verified", "snapshots_published",
            }
            and inventory_state.get("status") in {"pending", "verified"}
        )
        if checkpoint_phase in {"restore_failed", "restore_started"} or current_projection_recoverable:
            if not checkpoint_inventory_bound:
                return {"ok": False, "status": "pending", "stage": "generation_recovery",
                        "error_code": "XRAY_GENERATION_INVENTORY_ADMISSION_REQUIRED",
                        "last_good_retained": True}
            recovered = _recover_checkpoint_from_current_projection(adapter, checkpoint_path)
        else:
            recovered = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            if not recovered.get("ok") and recovered.get("recovered") == "stale_checkpoint_declined":
                recovered = (
                    _recover_checkpoint_from_current_projection(adapter, checkpoint_path)
                    if _checkpoint_inventory_generation_matches(checkpoint_path)
                    else {"ok": False, "recovered": "stale_checkpoint_declined",
                          "reason": "inventory_generation_not_bound"}
                )
        if not recovered.get("ok"):
            return {"ok": False, "status": "pending", "stage": "generation_recovery",
                    "error_code": "XRAY_GENERATION_RECOVERY_FAILED", "details": recovered,
                    "last_good_retained": True}
        return {"ok": True, "status": "recovered", **recovered}
    except Exception as exc:
        exception_name = type(exc).__name__
        safe_exception_name = (
            exception_name if len(exception_name) <= 64
            and exception_name.endswith(("Error", "Exception"))
            and exception_name.isascii()
            and exception_name.isalnum()
            else "recovery_exception"
        )
        return {"ok": False, "status": "pending", "stage": "generation_recovery",
                "error_code": "XRAY_GENERATION_RECOVERY_FAILED",
                "details": {"recovered": "current_projection_unproven", "reason": safe_exception_name},
                "last_good_retained": True}


def recover_xray_generation_checkpoint_before_inventory_refresh() -> dict[str, Any]:
    """Recover/prove any checkpoint before a provider inventory fetch begins."""
    try:
        with xray_writer_guard(timeout_seconds=30.0):
            adapter = _xray_adapter()
            checkpoint_path = _xray_generation_checkpoint_path(adapter)
            module = _module_state("xray") or {}
            managed_enabled = (
                str(module.get("desired_state") or "disabled") == "enabled"
                and str(module.get("lifecycle_mode") or "none") == "managed"
            )
            if not managed_enabled:
                if checkpoint_path.exists():
                    return {
                        "ok": False, "status": "blocked", "stage": "lifecycle",
                        "error_code": "XRAY_GENERATION_RECOVERY_REQUIRES_MANAGED_RUNTIME",
                        "last_good_retained": True,
                    }
                return {"ok": True, "status": "skipped", "reason": "xray_runtime_not_managed"}
            return _resolve_existing_xray_generation_checkpoint(adapter, checkpoint_path)
    except Exception:
        return {"ok": False, "status": "pending", "stage": "generation_recovery",
                "error_code": "XRAY_GENERATION_RECOVERY_FAILED", "last_good_retained": True}


def subscription_inventory_checkpoint_write_gate() -> dict[str, Any]:
    """Check-only guard for inventory writers; caller must own writer lock."""
    if not xray_writer_guard_is_held():
        return {"ok": False, "error_code": "XRAY_GENERATION_WRITER_GUARD_REQUIRED",
                "last_good_retained": True}
    try:
        adapter = _xray_adapter()
        checkpoint_path = _xray_generation_checkpoint_path(adapter)
        if not checkpoint_path.exists():
            return {"ok": True, "status": "clear"}
        return {"ok": False, "status": "pending", "stage": "generation_recovery",
                "error_code": "XRAY_GENERATION_CHECKPOINT_RECOVERY_REQUIRED",
                "last_good_retained": True}
    except Exception:
        return {"ok": False, "status": "pending", "stage": "generation_recovery",
                "error_code": "XRAY_GENERATION_CHECKPOINT_UNAVAILABLE", "last_good_retained": True}


def _capture_generation_derived_rows(
    *, managed_email_prefixes: list[str], expected_client_identities: list[list[str]] | list[tuple[str, str]],
) -> dict[str, Any]:
    prefixes = [str(value).lower() for value in managed_email_prefixes if value]
    expected_ids = {str(pair[0]) for pair in expected_client_identities if len(pair) >= 2 and pair[0]}
    expected_emails = {str(pair[1]).lower() for pair in expected_client_identities if len(pair) >= 2 and pair[1]}
    with db_session() as connection:
        subjects = [dict(row) for row in connection.execute(
            "SELECT * FROM subjects WHERE implementation_kind = 'xray' ORDER BY subject_id"
        ).fetchall()]
        selected = []
        for row in subjects:
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            detail = metadata.get("detail") if isinstance(metadata, dict) else {}
            detail = detail if isinstance(detail, dict) else {}
            client_id = str(detail.get("client_uuid") or detail.get("client_id") or "")
            email = str(detail.get("email") or "").lower()
            if client_id in expected_ids or email in expected_emails or any(email.startswith(prefix) for prefix in prefixes):
                selected.append(row)
        subject_ids = {str(row["subject_id"]) for row in selected}
        overrides = []
        user_overrides = []
        if subject_ids:
            placeholders = ", ".join("?" for _ in subject_ids)
            overrides = [dict(row) for row in connection.execute(
                f"SELECT * FROM subject_server_overrides WHERE subject_id IN ({placeholders}) ORDER BY subject_id",
                tuple(sorted(subject_ids)),
            ).fetchall()]
            user_overrides = [dict(row) for row in connection.execute(
                f"SELECT * FROM subject_user_overrides WHERE subject_id IN ({placeholders}) ORDER BY subject_id",
                tuple(sorted(subject_ids)),
            ).fetchall()]
    return {"subjects": selected, "server_overrides": overrides, "user_overrides": user_overrides}


def _restore_scoped_generation_rows(
    connection: Any, *, before: dict[str, Any], after: dict[str, Any],
) -> bool:
    """CAS-restore only projection rows changed by this generation."""
    scopes: dict[str, tuple[str, dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]] = {}
    for table_key, table in (
        ("subjects", "subjects"),
        ("server_overrides", "subject_server_overrides"),
        ("user_overrides", "subject_user_overrides"),
    ):
        before_rows = {str(row["subject_id"]): row for row in before.get(table_key, [])}
        after_rows = {str(row["subject_id"]): row for row in after.get(table_key, [])}
        scoped_ids = set(before_rows) | set(after_rows)
        current_rows = {
            str(row["subject_id"]): dict(row)
            for row in connection.execute(
                f"SELECT * FROM {table} WHERE subject_id IN ({', '.join('?' for _ in scoped_ids)})" if scoped_ids else f"SELECT * FROM {table} WHERE 0",
                tuple(sorted(scoped_ids)),
            ).fetchall()
        }
        expected_current = {key: after_rows[key] for key in after_rows}
        if current_rows != expected_current:
            return False
        if table_key == "user_overrides":
            for subject_id in set(before_rows) & set(after_rows):
                if before_rows[subject_id] != after_rows[subject_id]:
                    return False
        scopes[table_key] = (table, before_rows, after_rows, scoped_ids)

    # All three CAS checks finish before any write. Remove child rows first so
    # restoring/deleting subjects cannot cascade user-owned overrides.
    for table_key in ("server_overrides",):
        table, _, _, scoped_ids = scopes[table_key]
        for subject_id in scoped_ids:
            connection.execute(f"DELETE FROM {table} WHERE subject_id = ?", (subject_id,))
    _, before_user, after_user, _ = scopes["user_overrides"]
    for subject_id in set(after_user) - set(before_user):
        connection.execute("DELETE FROM subject_user_overrides WHERE subject_id = ?", (subject_id,))

    table, before_rows, after_rows, scoped_ids = scopes["subjects"]
    columns = [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]
    for subject_id in scoped_ids & set(before_rows) & set(after_rows):
        previous = before_rows[subject_id]
        names = [name for name in columns if name in previous and name != "subject_id"]
        connection.execute(
            f"UPDATE {table} SET {', '.join(f'{name} = ?' for name in names)} WHERE subject_id = ?",
            tuple(previous[name] for name in names) + (subject_id,),
        )
    for subject_id in set(after_rows) - set(before_rows):
        connection.execute(f"DELETE FROM {table} WHERE subject_id = ?", (subject_id,))
    for subject_id in set(before_rows) - set(after_rows):
        previous = before_rows[subject_id]
        names = [name for name in columns if name in previous]
        connection.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            tuple(previous[name] for name in names),
        )
    for table_key in ("server_overrides",):
        table, before_rows, _, _ = scopes[table_key]
        columns = [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]
        for previous in before_rows.values():
            names = [name for name in columns if name in previous]
            connection.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                tuple(previous[name] for name in names),
            )
    for subject_id in set(before_user) - set(after_user):
        previous = before_user[subject_id]
        columns = [str(row["name"]) for row in connection.execute("PRAGMA table_info(subject_user_overrides)").fetchall()]
        names = [name for name in columns if name in previous]
        connection.execute(
            f"INSERT INTO subject_user_overrides ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            tuple(previous[name] for name in names),
        )
    return True


def _write_xray_generation_checkpoint(
    *,
    adapter: Any,
    generation_id: str,
    tokens: set[str],
    phase: str,
    source_fingerprint: str,
    staged_generation: dict[str, Any],
    managed_email_prefixes: list[str],
    supersession: dict[str, Any] | None = None,
    inventory_generation: dict[str, Any] | None = None,
    fixed_workflow_revision: int | None = None,
    fixed_origin_selection_revision: int | None = None,
    ordinary_fixed_context: dict[str, Any] | None = None,
) -> Path:
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    fixed_routing_snapshot = _fixed_xray_routing_projection(
        (ordinary_fixed_context or {}).get("routing_snapshot")
        if ordinary_fixed_context is not None
        else staged_generation.get("fixed_routing_snapshot")
    )
    if fixed_workflow_revision is not None and fixed_routing_snapshot is None:
        raise RuntimeError("XRAY_FIXED_ROUTING_SNAPSHOT_UNAVAILABLE")

    checkpoint_path = _xray_generation_checkpoint_path(adapter)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.chmod(0o700)
    if checkpoint_path.exists():
        try:
            existing_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if _checkpoint_operation_mode(existing_checkpoint) == "proof_only_legacy_admission":
                raise RuntimeError("XRAY_PROOF_ONLY_CHECKPOINT_CANNOT_BE_SUPERSEDED")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, RuntimeError):
                raise
            # Invalid/unknown checkpoints must not be reinterpreted or
            # overwritten by an ordinary operation.
            raise RuntimeError("XRAY_GENERATION_CHECKPOINT_MODE_UNVERIFIABLE") from exc
    if checkpoint_path.exists() and supersession is None:
        raise RuntimeError("An unresolved Xray generation checkpoint already exists.")
    if supersession is not None:
        old_sha = str(supersession.get("checkpoint_sha256") or "")
        plan_sha = str(supersession.get("plan_sha256") or "")
        baseline = supersession.get("baseline")
        if (not checkpoint_path.is_file() or not isinstance(baseline, dict)
                or not _checkpoint_supersession_state_unchanged(adapter, checkpoint_path, baseline)
                or old_sha != baseline.get("checkpoint_sha256")
                or plan_sha != baseline.get("plan_sha256")):
            raise RuntimeError("XRAY_SUPERSESSION_CONTEXT_STALE")
        xray_candidate = Path(str(staged_generation.get("xray_candidate_path") or ""))
        xray_candidate_sha = str(staged_generation.get("xray_candidate_sha256") or "")
        mihomo_candidates = staged_generation.get("mihomo_candidates") or {}
        native_validation = staged_generation.get("native_validation") or {}
        if (not xray_candidate.is_file() or len(xray_candidate_sha) != 64
                or hashlib.sha256(xray_candidate.read_bytes()).hexdigest() != xray_candidate_sha):
            raise RuntimeError("XRAY_SUPERSESSION_CANDIDATE_CHANGED")
        for name in ("transition", "final"):
            candidate_path = Path(str(mihomo_candidates.get(name) or ""))
            expected = str(((native_validation.get(name) or {}).get("candidate_sha256")) or "")
            if (not candidate_path.is_file() or len(expected) != 64
                    or hashlib.sha256(candidate_path.read_bytes()).hexdigest() != expected):
                raise RuntimeError("XRAY_SUPERSESSION_CANDIDATE_CHANGED")
    xray_path = Path(adapter.config_path)
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    binding_path = _xray_bindings_path()
    with db_session() as connection:
        selection_fence = read_selection_fence(connection)
        ordinary_selection = (ordinary_fixed_context or {}).get("selection_snapshot")
        if isinstance(ordinary_fixed_context, dict):
            routing_row = connection.execute(
                "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
                "active_auto_server_id, desired_mode, applied_mode, selective_default, apply_state, updated_at "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            if (not isinstance(ordinary_selection, dict)
                    or selection_fence.get("revision") != ordinary_selection.get("selection_revision")
                    or selection_fence.get("decision_id") != ordinary_selection.get("selection_decision_id")
                    or _fixed_xray_routing_projection(dict(routing_row) if routing_row else None)
                        != fixed_routing_snapshot
                    or _legacy_admission_core_unavailable_reason(connection) is not None):
                raise RuntimeError("FIXED_XRAY_ROUTING_SNAPSHOT_STALE")
        snapshots: dict[str, dict[str, Any] | None] = {}
        for token in tokens:
            row = connection.execute(
                "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots WHERE token = ?",
                (token,),
            ).fetchone()
            snapshots[token] = dict(row) if row else None
    expected_client_identities = (
        (staged_generation.get("native_validation") or {}).get("xray", {}).get("expected_client_identities") or []
    )
    derived_rows = _capture_generation_derived_rows(
        managed_email_prefixes=managed_email_prefixes,
        expected_client_identities=expected_client_identities,
    )
    data = {
        "version": 1,
        "generation_id": generation_id,
        "phase": phase,
        "source_fingerprint": source_fingerprint,
        "fixed_workflow_revision": fixed_workflow_revision,
        "fixed_origin_selection_revision": fixed_origin_selection_revision,
        "subscription_inventory_generation": dict(
            inventory_generation if inventory_generation is not None else _current_inventory_generation_binding()
        ),
        "created_at": time.time(),
        "selection_operation_id": generation_id,
        "selection_revision": selection_fence["revision"],
        "mihomo_runtime_incarnation": get_mihomo_runtime_incarnation(),
        "xray_runtime_incarnation_before": (
            adapter.get_runtime_incarnation() if callable(getattr(adapter, "get_runtime_incarnation", None)) else None
        ),
        "xray_mounted_config_sha256_before": (
            adapter.get_runtime_config_sha256() if callable(getattr(adapter, "get_runtime_config_sha256", None)) else None
        ),
        "artifacts": {
            "xray_config": {"path": str(xray_path), "text": base64.b64encode(xray_path.read_bytes()).decode("ascii") if xray_path.exists() else None},
            "mihomo_config": {"path": str(mihomo_path), "text": base64.b64encode(mihomo_path.read_bytes()).decode("ascii") if mihomo_path.exists() else None},
            "xray_bindings": {
                "path": str(binding_path),
                "text": base64.b64encode(binding_path.read_bytes()).decode("ascii") if binding_path.exists() else None,
            },
        },
        "subscription_snapshots": snapshots,
        "derived_rows_before": derived_rows,
        "derived_rows_after": None,
        "auto_selection_before": (
            ordinary_selection if isinstance(ordinary_selection, dict)
            else _capture_generation_auto_selection()
        ),
        "auto_selection_after": None,
        "managed_email_prefixes": managed_email_prefixes,
        "fixed_routing_snapshot": (
            fixed_routing_snapshot if fixed_routing_snapshot is not None else None
        ),
        "ordinary_fixed_core_proof": (
            dict(ordinary_fixed_context.get("core_proof") or {})
            if isinstance(ordinary_fixed_context, dict) else None
        ),
        "ordinary_fixed_core_runtime_fingerprint": (
            ordinary_fixed_context.get("core_runtime_fingerprint")
            if isinstance(ordinary_fixed_context, dict) else None
        ),
        "expected_client_identities": expected_client_identities,
        "staged_generation": {
            "xray_candidate_path": staged_generation.get("xray_candidate_path"),
            "xray_candidate_sha256": staged_generation.get("xray_candidate_sha256"),
            "mihomo_transition_path": (staged_generation.get("mihomo_candidates") or {}).get("transition"),
            "mihomo_transition_sha256": ((staged_generation.get("native_validation") or {}).get("transition") or {}).get("candidate_sha256"),
            "mihomo_final_path": (staged_generation.get("mihomo_candidates") or {}).get("final"),
            "mihomo_final_sha256": ((staged_generation.get("native_validation") or {}).get("final") or {}).get("candidate_sha256"),
        },
    }
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
    data["native_validation_proof"] = {
        "xray": {
            "ok": xray_receipt.get("stage") == "validated",
            "candidate_sha256": xray_receipt.get("candidate_sha256"),
            "expected_client_identities_sha256": hashlib.sha256(identity_receipt_bytes).hexdigest(),
        },
        "mihomo": mihomo_receipts,
    }
    if supersession is not None:
        archive_path = _archive_checkpoint_exact(checkpoint_path, str(supersession["checkpoint_sha256"]))
        data["supersession"] = {
            "checkpoint_sha256": str(supersession["checkpoint_sha256"]),
            "archive_path": str(archive_path),
            "migration_plan_sha256": str(supersession["plan_sha256"]),
            "migration_transition_count": _identity_migration_ledger_matches(
                plan_sha256=str(supersession["plan_sha256"]),
                checkpoint_sha256=str(supersession["checkpoint_sha256"]),
            ),
            "baseline": supersession["baseline"],
            "rollback_preimage_projection_matches": bool(
                supersession["baseline"].get("rollback_preimage_projection_matches")
            ),
        }
        if not _checkpoint_supersession_state_unchanged(adapter, checkpoint_path, supersession["baseline"]):
            raise RuntimeError("XRAY_SUPERSESSION_CONTEXT_STALE")
    _atomic_write_private_checkpoint(checkpoint_path, json.dumps(data, sort_keys=True))
    return checkpoint_path


def _update_xray_generation_checkpoint(
    checkpoint_path: Path, *, phase: str, selection_revision: int | None = None,
    xray_runtime_incarnation: str | None = None, xray_mounted_config_sha256: str | None = None,
) -> None:
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    data["phase"] = phase
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    data["mihomo_runtime_incarnation"] = get_mihomo_runtime_incarnation()
    if selection_revision is not None:
        if type(selection_revision) is not int or selection_revision < int(data.get("selection_revision", 0)):
            raise ValueError("Generation checkpoint cannot adopt an invalid or older selection revision.")
        data["selection_revision"] = selection_revision
    if xray_runtime_incarnation is not None:
        data["xray_runtime_incarnation_after"] = xray_runtime_incarnation
    if xray_mounted_config_sha256 is not None:
        data["xray_mounted_config_sha256_after"] = xray_mounted_config_sha256
    _atomic_write_private_checkpoint(checkpoint_path, json.dumps(data, sort_keys=True))


def _record_xray_generation_selection_revision(
    checkpoint_path: Path, selection_revision: int | None,
) -> None:
    """Persist only the exact revision returned by this staged promotion."""
    if type(selection_revision) is not int:
        raise ValueError("Staged Mihomo promotion did not return its selection revision.")
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    current = data.get("selection_revision")
    if type(current) is not int or selection_revision < current:
        raise ValueError("Staged Mihomo promotion returned an older selection revision.")
    data["selection_revision"] = selection_revision
    _atomic_write_private_checkpoint(checkpoint_path, json.dumps(data, sort_keys=True))


def _capture_generation_auto_selection() -> dict[str, Any]:
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    with db_session() as connection:
        routing = connection.execute(
            "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
            "active_auto_server_id, desired_mode, applied_mode, selective_default, apply_state, updated_at "
            "FROM routing_global_state WHERE id = 1"
        ).fetchone()
        provenance = connection.execute(
            "SELECT value_json, updated_at FROM settings WHERE key = 'routing.auto_selection_provenance'"
        ).fetchone()
        fence = read_selection_fence(connection)
    return {
        "routing": dict(routing) if routing else None,
        "provenance": dict(provenance) if provenance else None,
        "selection_revision": fence["revision"],
        "selection_decision_id": fence["decision_id"],
    }


def _validate_rollback_mihomo_preimage(
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


def _restore_generation_auto_selection(*, before: dict[str, Any], after: dict[str, Any], operation_id: str | None = None) -> bool:
    from fwrouter_api.services.selector import restore_auto_selection_snapshot

    return bool(restore_auto_selection_snapshot(before=before, after=after, operation_id=operation_id).get("ok"))


def _verify_generation_selection_readback(selection: dict[str, Any] | None) -> dict[str, Any]:
    routing = (selection or {}).get("routing") if isinstance(selection, dict) else None
    if not isinstance(routing, dict):
        return {"ok": False, "error_code": "SELECTION_BASELINE_UNAVAILABLE"}
    mode = str(routing.get("server_mode") or "auto").lower()
    logical_id = str(
        routing.get("desired_fixed_server_id") or routing.get("applied_fixed_server_id") or ""
        if mode == "fixed"
        else routing.get("active_auto_server_id") or ""
    ).strip()
    if not logical_id:
        return {"ok": False, "error_code": "SELECTION_BASELINE_UNSELECTED"}
    from fwrouter_api.services.logical_topology import get_logical_runtime_name
    from fwrouter_api.services.selector import get_vpn_auto_state
    from fwrouter_api.services.servers import get_routing_global_state
    state = get_vpn_auto_state(read_only=True)
    current_routing = get_routing_global_state(expire_ttl=False) or {}
    selectors = state.get("selector_runtime") if isinstance(state.get("selector_runtime"), dict) else {}
    selector_name = "vpn_global_now" if mode == "fixed" else "vpn_auto_now"
    expected = get_logical_runtime_name(logical_id)
    observed = str(selectors.get(selector_name) or "").strip()
    current_logical = (
        str(current_routing.get("desired_fixed_server_id") or current_routing.get("applied_fixed_server_id") or "").strip()
        if mode == "fixed"
        else str(state.get("active_auto_server_id") or "").strip()
    )
    ok = bool(expected and observed and current_logical == logical_id and observed == expected)
    return {
        "ok": ok,
        "mode": mode,
        "logical_server_id": logical_id,
        "expected_effective_target": expected,
        "effective_target": observed or None,
        "error_code": None if ok else "SELECTION_RESTORE_READBACK_UNCONFIRMED",
    }


def _verify_generation_recovery_selection_readback(selection: dict[str, Any] | None) -> dict[str, Any]:
    """Verify selection readback, including an explicitly committed global-DIRECT intent.

    Recovery may legitimately run with no selected VPN target while global DIRECT is
    applied. That case is accepted only when the checkpoint captured the exact DIRECT
    intent/fence and fresh native dataplane readback proves the same mode. Older
    checkpoints lacking the intent fields remain unprovable and fail closed.
    """
    ordinary = _verify_generation_selection_readback(selection)
    if ordinary.get("ok") is True:
        return ordinary
    if ordinary.get("error_code") != "SELECTION_BASELINE_UNSELECTED":
        return ordinary

    captured = selection if isinstance(selection, dict) else {}
    captured_routing = captured.get("routing") if isinstance(captured.get("routing"), dict) else {}
    captured_mode = str(captured_routing.get("desired_mode") or "").strip().lower()
    if (
        captured_mode != "direct"
        or str(captured_routing.get("applied_mode") or "").strip().lower() != "direct"
        or str(captured_routing.get("apply_state") or "").strip().lower() != "clean"
    ):
        return {"ok": False, "error_code": "SELECTION_DIRECT_INTENT_UNPROVEN"}

    try:
        from fwrouter_api.services.servers import get_routing_global_state
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

        current_routing = get_routing_global_state(expire_ttl=False) or {}
        current_fence = _current_xray_projection_snapshot().get("fence") or {}
        expected_routing = {
            key: captured_routing.get(key)
            for key in (
                "server_mode", "desired_fixed_server_id", "applied_fixed_server_id",
                "active_auto_server_id", "desired_mode", "applied_mode", "apply_state",
            )
        }
        observed_routing = {key: current_routing.get(key) for key in expected_routing}
        with db_session() as connection:
            durable_fence = read_selection_fence(connection)
        no_vpn_target = not any(
            str(value or "").strip()
            for value in (
                captured_routing.get("desired_fixed_server_id"),
                captured_routing.get("applied_fixed_server_id"),
                captured_routing.get("active_auto_server_id"),
                current_routing.get("desired_fixed_server_id"),
                current_routing.get("applied_fixed_server_id"),
                current_routing.get("active_auto_server_id"),
                current_fence.get("active_server_id"),
                current_fence.get("provenance_server_id"),
                durable_fence.get("active_server_id"),
                durable_fence.get("provenance_server_id"),
            )
        )
        checkpoint_decision = captured.get("selection_decision_id")
        if (
            observed_routing != expected_routing
            or type(captured.get("selection_revision")) is not int
            or captured.get("selection_revision") != current_fence.get("revision")
            or captured.get("selection_revision") != durable_fence.get("revision")
            or "selection_decision_id" not in captured
            or checkpoint_decision != current_fence.get("decision_id")
            or checkpoint_decision != durable_fence.get("decision_id")
            or (checkpoint_decision is None and not no_vpn_target)
            or (checkpoint_decision is not None and not str(checkpoint_decision).strip())
        ):
            return {"ok": False, "error_code": "SELECTION_DIRECT_FENCE_UNPROVEN"}

        # Use the existing live dataplane/enforcement readbacks. Refresh their
        # existing short-lived probe keys so stale cached state cannot verify a
        # checkpoint after restart.
        from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status
        from fwrouter_api.services.live_probe_cache import get_live_probe_cache

        payload = dataplane_status._read_live_dataplane_payload()
        if not dataplane_status._live_owned_table_ready(payload):
            return {"ok": False, "error_code": "SELECTION_DIRECT_NATIVE_READBACK_UNREADY"}
        manifest = get_live_probe_cache(
            "dataplane_global.applied_manifest", ttl_seconds=5.0,
            loader=dataplane_global._read_applied_manifest_uncached, force_refresh=True,
        )
        get_live_probe_cache(
            "dataplane_live.global_mode", ttl_seconds=2.0,
            loader=dataplane_live._probe_live_global_mode_uncached, force_refresh=True,
        )
        manifest_routing = manifest.get("routing_global_state") if isinstance(manifest, dict) else None
        if not isinstance(manifest_routing, dict) or any(
            str(manifest_routing.get(key) or "").strip().lower() != "direct"
            for key in ("desired_mode", "applied_mode")
        ):
            return {"ok": False, "error_code": "SELECTION_DIRECT_APPLIED_MANIFEST_UNPROVEN"}
        proof = dataplane_status.build_runtime_enforcement_state(live_payload=payload)
        if (
            not isinstance(proof, dict)
            or proof.get("enforcement_level") != "global_direct_enforced"
            or proof.get("traffic_enforcement_guaranteed") is not True
            or proof.get("active_mode_matches_intent") is not True
            or str(proof.get("live_global_mode") or "").strip().lower() != "direct"
        ):
            return {"ok": False, "error_code": "SELECTION_DIRECT_NATIVE_POLICY_UNPROVEN"}
        return {
            "ok": True,
            "mode": "direct",
            "logical_server_id": None,
            "native_enforcement_level": proof["enforcement_level"],
        }
    except Exception:
        return {"ok": False, "error_code": "SELECTION_DIRECT_NATIVE_READBACK_FAILED"}


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


def _generation_recovery_attempt_path(checkpoint_path: Path, checkpoint_digest: str) -> Path:
    return checkpoint_path.with_name(f"generation-recovery-attempt-{checkpoint_digest}.json")


def _current_xray_projection_snapshot() -> dict[str, Any]:
    """Read current committed Xray identities, snapshots, and writer fence."""
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    with db_session() as connection:
        rows = connection.execute(
            """SELECT json_extract(metadata_json, '$.detail.client_uuid') AS client_uuid,
                      json_extract(metadata_json, '$.detail.client_id') AS client_id,
                      json_extract(metadata_json, '$.detail.email') AS email
               FROM subjects WHERE implementation_kind='xray' AND is_active=1 AND is_deleted=0
                 AND COALESCE(json_extract(metadata_json, '$.detail.enabled'), 1)=1
               ORDER BY subject_id"""
        ).fetchall()
        snapshots = [dict(row) for row in connection.execute(
            "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots ORDER BY token"
        ).fetchall()]
        published_snapshots = [dict(row) for row in connection.execute(
            """SELECT p.token, p.nodes_json, p.runtime_verified_at, p.updated_at
               FROM subscription_profile_snapshots AS p
               JOIN subscription_clients AS c ON c.token=p.token AND c.enabled=1
               JOIN subscription_accounts AS a ON a.account_id=c.account_id AND a.enabled=1
               WHERE p.runtime_verified_at IS NOT NULL ORDER BY p.token"""
        ).fetchall()]
        fence = read_selection_fence(connection)
    identities: list[tuple[str, str]] = []
    for row in rows:
        client_id = str(row["client_uuid"] or row["client_id"] or "").strip()
        email = str(row["email"] or "").strip()
        if not client_id or not email:
            raise ValueError("current_xray_identity_incomplete")
        identities.append((client_id, email))
    if len(set(identities)) != len(identities):
        raise ValueError("current_xray_identity_duplicate")
    return {"identities": sorted(identities), "snapshots": snapshots,
            "published_snapshots": published_snapshots, "fence": fence}


def _current_projection_fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        {"identities": snapshot["identities"], "snapshots": snapshot["snapshots"],
         "fence": snapshot["fence"], "source": _generation_source_fingerprint(),
         "selection": _capture_generation_auto_selection()},
        sort_keys=True, default=str,
    ).encode()).hexdigest()


def _current_bindings_artifact_parity(bindings: list[dict[str, Any]], modes: list[dict[str, Any]]) -> tuple[bool, str]:
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    from fwrouter_api.services.xray_bindings import _bindings_for_state, get_xray_handoff_listeners

    path = _xray_bindings_path()
    try:
        raw = path.read_bytes()
        state = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False, ""
    if not isinstance(state, dict) or state.get("error_code"):
        return False, hashlib.sha256(raw).hexdigest()
    binding_keys = (
        "subject_id", "client_id", "client_uuid", "client_email", "selected_server_id",
        "selected_server_source", "handoff_proxy_name", "server_name", "server_runtime_name",
        "match_key", "handoff",
    )
    mode_keys = ("subject_id", "client_id", "client_uuid", "client_email", "desired_mode", "effective_mode", "mode_support_state")
    def normalized(items: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        return sorted(({key: item.get(key) for key in keys} for item in items if isinstance(item, dict)),
                      key=lambda item: (str(item.get("client_email") or ""), str(item.get("subject_id") or "")))

    def normalized_handoffs(items: Any) -> list[dict[str, Any]] | None:
        if not isinstance(items, list):
            return None
        normalized_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                return None
            normalized_item = dict(item)
            for key in ("subject_ids", "client_emails"):
                if key not in normalized_item:
                    continue
                values = normalized_item[key]
                if not isinstance(values, list):
                    return None
                try:
                    # These fields are membership associations. Canonicalize
                    # order while retaining JSON types and duplicate counts.
                    normalized_item[key] = sorted(
                        values,
                        key=lambda value: json.dumps(
                            value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False,
                        ),
                    )
                except (TypeError, ValueError):
                    return None
            normalized_items.append(normalized_item)
        try:
            return sorted(
                normalized_items,
                key=lambda item: json.dumps(
                    item, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False,
                ),
            )
        except (TypeError, ValueError):
            return None

    expected_bindings = _bindings_for_state(bindings)
    expected_modes = [{key: item.get(key) for key in mode_keys} for item in modes]
    expected_handoffs = get_xray_handoff_listeners(bindings)
    actual_handoffs_normalized = normalized_handoffs(state.get("handoff_listeners"))
    expected_handoffs_normalized = normalized_handoffs(expected_handoffs)
    handoff_parity = bool(
        actual_handoffs_normalized is not None
        and expected_handoffs_normalized is not None
        and json.dumps(actual_handoffs_normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        == json.dumps(expected_handoffs_normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    )
    parity = bool(
        state.get("bindings_version") == 1
        and normalized(state.get("bindings"), binding_keys) == normalized(expected_bindings, binding_keys)
        and normalized(state.get("client_modes"), mode_keys) == normalized(expected_modes, mode_keys)
        and handoff_parity
        and state.get("bindings_count") == len(bindings)
        and state.get("applied_count") == len(bindings)
        and all(item.get("status") == "applied" for item in state.get("bindings", []) if isinstance(item, dict))
        and state.get("client_modes_count") == len(modes)
        and all(item.get("status") == "applied" for item in state.get("client_modes", []) if isinstance(item, dict))
    )
    return parity, hashlib.sha256(raw).hexdigest()


def _verify_current_public_projection(
    snapshot: dict[str, Any], *, supersession: dict[str, Any] | None = None,
) -> bool:
    """Require every published node to remain a member of current committed Xray identities."""
    identities = set(snapshot["identities"])
    from fwrouter_api.services.custom_servers import VIRTUAL_XRAY_VPN_AUTO_SERVER_ID

    auto_selection = _capture_generation_auto_selection()
    routing = auto_selection.get("routing") or {}
    active_auto = str(routing.get("active_auto_server_id") or "").strip()
    binding_by_identity = {
        (str(item.get("client_uuid") or item.get("client_id") or "").strip(),
         str(item.get("client_email") or "").strip()): item
        for item in collect_xray_runtime_bindings()
    }
    for row in snapshot["published_snapshots"]:
        try:
            nodes = json.loads(row.get("nodes_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(nodes, list):
            return False
        for node in nodes:
            if not isinstance(node, dict):
                return False
            identity = (str(node.get("client_uuid") or "").strip(), str(node.get("client_email") or "").strip())
            if identity not in identities:
                return False
            binding = binding_by_identity.get(identity)
            if binding is None:
                return False
            public_server_id = str(node.get("server_id") or "").strip()
            bound_server_id = str(binding.get("selected_server_id") or "").strip()
            if public_server_id == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
                if (str(binding.get("selected_server_id") or "").strip() != "vpn-global"
                        or str(binding.get("selected_server_source") or "").strip() != "vpn_auto"
                        or not active_auto):
                    return False
            else:
                if bound_server_id != public_server_id and not _supersession_alias_owns(
                    public_server_id, bound_server_id, supersession,
                ):
                    return False
    return True


def _supersession_alias_owns(
    public_server_id: str, bound_server_id: str, supersession: dict[str, Any] | None,
) -> bool:
    if not isinstance(supersession, dict):
        return False
    checkpoint_sha = str(supersession.get("checkpoint_sha256") or "")
    plan_sha = str(supersession.get("migration_plan_sha256") or "")
    if len(checkpoint_sha) != 64 or len(plan_sha) != 64:
        return False
    with db_session() as connection:
        row = connection.execute(
            """SELECT 1 FROM subscription_node_aliases AS old
               JOIN subscription_node_aliases AS new ON new.identity_id=old.identity_id
               JOIN subscription_node_identity_transitions AS t ON t.identity_id=old.identity_id
               WHERE old.server_id=? AND new.server_id=? AND t.from_server_id=old.server_id
                 AND t.to_server_id=new.server_id AND t.operator_plan_sha256=?
                 AND t.checkpoint_sha256=? LIMIT 1""",
            (public_server_id, bound_server_id, plan_sha, checkpoint_sha),
        ).fetchone()
    return row is not None


def _supersession_current_candidate_owned(
    adapter: Any, checkpoint: dict[str, Any], projection: dict[str, Any],
) -> bool:
    supersession = checkpoint.get("supersession")
    staged = checkpoint.get("staged_generation")
    after_selection = checkpoint.get("auto_selection_after")
    if not isinstance(supersession, dict) or not isinstance(staged, dict):
        return False
    phase = str(checkpoint.get("phase") or "")
    if phase not in {"runtime_applied", "inventory_synced", "bindings_written",
                     "projections_cleaned", "selection_verified", "restore_failed"}:
        return False
    expected_xray = str(staged.get("xray_candidate_sha256") or "")
    expected_mihomo = str(staged.get("mihomo_final_sha256") or "")
    proof = checkpoint.get("native_validation_proof")
    xray_proof = proof.get("xray") if isinstance(proof, dict) else None
    mihomo_proof = proof.get("mihomo") if isinstance(proof, dict) else None
    if (not isinstance(xray_proof, dict) or xray_proof.get("ok") is not True
            or xray_proof.get("candidate_sha256") != expected_xray
            or not isinstance(mihomo_proof, dict)):
        return False
    for name in ("transition", "final"):
        item = mihomo_proof.get(name)
        candidate_sha = str(staged.get(f"mihomo_{name}_sha256") or "")
        receipt_sha = str(item.get("receipt_sha256") or "") if isinstance(item, dict) else ""
        if (not isinstance(item, dict) or item.get("ok") is not True
                or item.get("candidate_sha256") != candidate_sha
                or len(receipt_sha) != 64
                or any(char not in "0123456789abcdef" for char in receipt_sha)):
            return False
    if len(expected_xray) != 64 or len(expected_mihomo) != 64:
        return False
    try:
        from fwrouter_api.services import mihomo_config
        xray_path = Path(adapter.config_path)
        mihomo_path = Path(mihomo_config._resolved_base_config_path())
        current_xray_sha = hashlib.sha256(xray_path.read_bytes()).hexdigest()
        current_mihomo_sha = hashlib.sha256(mihomo_path.read_bytes()).hexdigest()
        if (current_xray_sha != expected_xray
                or adapter.get_runtime_config_sha256() != expected_xray
                or current_mihomo_sha != expected_mihomo):
            return False
        if (not checkpoint.get("xray_runtime_incarnation_after")
                or adapter.get_runtime_incarnation() != checkpoint.get("xray_runtime_incarnation_after")):
            return False
        from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
        if (not checkpoint.get("mihomo_runtime_incarnation")
                or get_mihomo_runtime_incarnation() != checkpoint.get("mihomo_runtime_incarnation")):
            return False
        fence = projection.get("fence") or {}
        if checkpoint.get("selection_revision") != fence.get("revision"):
            return False
        if isinstance(after_selection, dict):
            if (after_selection.get("selection_revision") != fence.get("revision")
                    or after_selection.get("selection_decision_id") != fence.get("decision_id")):
                return False
        expected_source = checkpoint.get("derived_source_fingerprint") or checkpoint.get("source_fingerprint")
        if expected_source != _generation_source_fingerprint():
            return False
        if sorted(adapter.list_loaded_client_identities()) != sorted(projection["identities"]):
            return False
    except Exception:
        return False
    return True


def _recover_checkpoint_from_current_projection(adapter: Any, checkpoint_path: Path) -> dict[str, Any]:
    """Apply and prove the already committed Xray projection before closing a stale marker.

    This never restores checkpoint artifacts, writes selection state, or restarts Mihomo.
    """
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.artifacts import atomic_write_text
    from fwrouter_api.adapters.xray_common import _json_dump
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.xray_materialize import (
        _verify_active_config_bindings, _verify_active_config_client_modes,
    )

    attempt_path: Path | None = None
    try:
        checkpoint_digest = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
        attempt_path = _generation_recovery_attempt_path(checkpoint_path, checkpoint_digest)
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        mode = _checkpoint_operation_mode(checkpoint)
        if mode == "proof_only_legacy_admission":
            return {"ok": False, "recovered": "proof_only_admission_pending",
                    "reason": "proof_only_operation_requires_explicit_resume"}
        phase = str(checkpoint.get("phase") or "")
        recoverable_phases = {
            "runtime_applied", "inventory_synced", "bindings_written",
            "projections_cleaned", "selection_verified", "snapshots_published",
            "restore_failed", "restore_started",
        }
        if phase not in recoverable_phases:
            return {"ok": False, "recovered": "current_projection_unproven",
                    "reason": "checkpoint_phase_not_recoverable"}
        supersession = checkpoint.get("supersession") if isinstance(checkpoint.get("supersession"), dict) else None
        previous_attempt = None
        if attempt_path.exists():
            previous_attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            if (not isinstance(previous_attempt, dict)
                    or previous_attempt.get("checkpoint_sha256") != checkpoint_digest):
                return {"ok": False, "recovered": "current_projection_unproven", "reason": "recovery_attempt_mismatch"}
            if previous_attempt.get("phase") not in {"prepared", "reload_started", "superseded_verified"}:
                return {"ok": False, "recovered": "current_projection_unproven", "reason": "recovery_attempt_phase_unknown"}
        generation_id = str(checkpoint.get("generation_id") or "")
        from fwrouter_api.services.subscription import get_subscription_native_generation_state
        inventory_state = get_subscription_native_generation_state()
        inventory_binding = checkpoint.get("subscription_inventory_generation")
        if inventory_state.get("status") == "empty":
            if inventory_binding is not None and (
                not isinstance(inventory_binding, dict)
                or inventory_binding.get("digest") is not None
                or inventory_binding.get("revision") is not None
            ):
                return {"ok": False, "recovered": "current_projection_unproven",
                        "reason": "inventory_checkpoint_binding_mismatch"}
        elif inventory_state.get("status") in {"pending", "verified"}:
            if (
                not isinstance(inventory_binding, dict)
                or inventory_binding.get("digest") != inventory_state.get("candidate_digest")
                or type(inventory_binding.get("revision")) is not int
                or inventory_binding.get("revision") != inventory_state.get("candidate_revision")
            ):
                return {"ok": False, "recovered": "current_projection_unproven",
                        "reason": "inventory_checkpoint_binding_mismatch"}
        else:
            return {"ok": False, "recovered": "current_projection_unproven",
                    "reason": "inventory_generation_unverified"}
        projection = _current_xray_projection_snapshot()
        supersession_owned = bool(
            isinstance(supersession, dict)
            and not supersession.get("rollback_preimage_projection_matches")
            and _supersession_current_candidate_owned(adapter, checkpoint, projection)
        )
        if (isinstance(supersession, dict)
                and not supersession.get("rollback_preimage_projection_matches")
                and not supersession_owned):
            return {"ok": False, "recovered": "current_projection_unproven",
                    "reason": "supersession_current_candidate_unowned"}
        bindings = collect_xray_runtime_bindings()
        modes = collect_xray_client_mode_directives()
        expected = sorted((str(a), str(b)) for a, b in projection["identities"])
        binding_identities = {
            (str(item.get("client_uuid") or item.get("client_id") or "").strip(),
             str(item.get("client_email") or "").strip()) for item in bindings
        }
        mode_identities = {
            (str(item.get("client_uuid") or item.get("client_id") or "").strip(),
             str(item.get("client_email") or "").strip()) for item in modes
        }
        if (not expected or (binding_identities | mode_identities) != set(expected)
                or not _verify_current_public_projection(
                    projection, supersession=supersession if supersession_owned else None,
                )):
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "committed_projection_mismatch"}
        artifact_parity, bindings_digest = _current_bindings_artifact_parity(bindings, modes)
        if not artifact_parity:
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "bindings_artifact_mismatch"}

        config_path = Path(adapter.config_path)
        before_config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
        before_incarnation = adapter.get_runtime_incarnation()
        before_mounted_hash = adapter.get_runtime_config_sha256()
        if before_mounted_hash != before_config_hash:
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "mounted_config_mismatch"}
        resume_owned_restart = False
        if isinstance(previous_attempt, dict) and previous_attempt.get("phase") == "prepared":
            if (before_config_hash != str(previous_attempt.get("xray_before_config_sha256") or "")
                    or before_incarnation != str(previous_attempt.get("xray_before_incarnation") or "")):
                return {"ok": False, "recovered": "current_projection_unproven", "reason": "ambiguous_prior_prepare"}
        if isinstance(previous_attempt, dict) and previous_attempt.get("phase") in {"reload_started", "superseded_verified"}:
            attempted_incarnation = str(previous_attempt.get("xray_before_incarnation") or "")
            expected_post_incarnation = str(previous_attempt.get("observed_runtime_incarnation") or "")
            if (before_config_hash != str(previous_attempt.get("xray_candidate_sha256") or "")
                    or not attempted_incarnation or before_incarnation == attempted_incarnation
                    or (expected_post_incarnation and before_incarnation != expected_post_incarnation)):
                return {"ok": False, "recovered": "current_projection_unproven", "reason": "ambiguous_prior_reload"}
            resume_owned_restart = True
        mihomo_path = Path(mihomo_config._resolved_base_config_path())
        mihomo_hash = hashlib.sha256(mihomo_path.read_bytes()).hexdigest()
        if isinstance(inventory_binding, dict) and inventory_binding.get("digest") is not None:
            expected_mihomo_candidate = str(
                ((checkpoint.get("staged_generation") or {}).get("mihomo_final_sha256")) or ""
            )
            if not expected_mihomo_candidate or mihomo_hash != expected_mihomo_candidate:
                return {"ok": False, "recovered": "current_projection_unproven",
                        "reason": "inventory_mihomo_checkpoint_mismatch"}
        mihomo_incarnation = get_mihomo_runtime_incarnation()
        source_fingerprint = _generation_source_fingerprint()
        projection_fingerprint = _current_projection_fingerprint(projection)
        current_selection = _capture_generation_auto_selection()
        selection = _checkpoint_selection_for_recovery(checkpoint, current_selection)
        if not isinstance(selection, dict):
            return {"ok": False, "recovered": "current_projection_unproven",
                    "reason": "checkpoint_selection_intent_missing"}
        from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
        from fwrouter_api.services.xray_bindings import get_xray_handoff_listeners
        handoffs = get_xray_handoff_listeners(bindings)
        if any(not DEFAULT_MIHOMO_ADAPTER.check_port(
                int(item.get("port") or 0), host="172.18.0.1", timeout=1.0)
               for item in handoffs):
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "handoff_listener_unready"}

        payload, inbound, _ = adapter._load_clients_and_config()
        current_api = [item for item in payload.get("inbounds", [])
                       if isinstance(item, dict) and str(item.get("tag") or "") == "fwrouter-api"]
        if current_api and (len(current_api) != 1 or current_api[0] != adapter._managed_api_inbound()):
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "api_listener_unsafe"}
        clients = list((inbound.get("settings") or {}).get("clients") or [])
        current_identities = sorted(
            (str(item.get("id") or "").strip(), str(item.get("email") or "").strip())
            for item in clients if isinstance(item, dict) and item.get("id") and item.get("email")
        )
        if current_identities != expected:
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "config_identity_mismatch"}

        updated_clients, _ = adapter._materialize_client_binding_metadata(
            raw_clients=clients, bindings=bindings,
        )
        inbound.setdefault("settings", {})["clients"] = updated_clients
        adapter._ensure_runtime_stats(payload)
        _, egress = adapter._materialize_managed_egress(
            payload=payload, bindings=bindings, client_modes=modes,
        )
        candidate_path = adapter._candidate_path()
        candidate_text = _json_dump(payload)
        atomic_write_text(candidate_path, candidate_text)
        candidate_sha = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
        candidate_path.chmod(0o600)
        native = adapter.test_config(str(candidate_path))
        if not native.ok or hashlib.sha256(candidate_path.read_bytes()).hexdigest() != candidate_sha:
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "native_candidate_invalid"}
        if (supersession_owned
                and candidate_sha != str((checkpoint.get("staged_generation") or {}).get("xray_candidate_sha256") or "")):
            return {"ok": False, "recovered": "current_projection_unproven",
                    "reason": "supersession_candidate_rebuild_mismatch"}

        operation_id = (str(previous_attempt.get("operation_id")) if isinstance(previous_attempt, dict)
                        else uuid4().hex)
        backup_name = (str(previous_attempt.get("xray_backup_name")) if isinstance(previous_attempt, dict)
                       else f"recovery-{operation_id}-xray-config.backup")
        backup_path = attempt_path.with_name(backup_name)
        backup_sha = (str(previous_attempt.get("xray_backup_sha256")) if isinstance(previous_attempt, dict)
                      else before_config_hash)
        if backup_path.exists():
            backup_bytes = backup_path.read_bytes()
            if hashlib.sha256(backup_bytes).hexdigest() != backup_sha:
                return {"ok": False, "recovered": "current_projection_unproven", "reason": "recovery_backup_mismatch"}
        elif isinstance(previous_attempt, dict):
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "recovery_backup_missing"}
        else:
            atomic_write_text(backup_path, config_path.read_text(encoding="utf-8"))
            backup_path.chmod(0o600)
            _fsync_directory(backup_path.parent)
        attempt = {
            "version": 1, "phase": "prepared", "generation_id": generation_id,
            "operation_id": operation_id, "checkpoint_sha256": checkpoint_digest,
            "source_fingerprint": source_fingerprint,
            "projection_fingerprint": projection_fingerprint,
            "selection_fence": projection["fence"],
            "mihomo_incarnation": mihomo_incarnation, "mihomo_config_sha256": mihomo_hash,
            "xray_before_incarnation": (str(previous_attempt.get("xray_before_incarnation"))
                                         if resume_owned_restart else before_incarnation),
            "xray_before_config_sha256": before_config_hash,
            "xray_before_mounted_sha256": before_mounted_hash,
            "xray_candidate_sha256": candidate_sha,
            "xray_backup_sha256": backup_sha,
            "xray_backup_name": backup_path.name,
            "bindings_artifact_sha256": bindings_digest,
            "expected_identity_count": len(expected),
            "expected_binding_count": len(bindings), "expected_mode_count": len(modes),
            "egress_count": int(egress.get("egress_count") or 0), "created_at": time.time(),
        }

        def save_attempt(phase: str, **extra: Any) -> None:
            attempt["phase"] = phase
            attempt.update(extra)
            atomic_write_text(attempt_path, json.dumps(attempt, sort_keys=True))
            attempt_path.chmod(0o600)
            _fsync_directory(attempt_path.parent)

        def stable(*, applied: bool = False, runtime_incarnation: str | None = None) -> bool:
            try:
                current = _current_xray_projection_snapshot()
                return bool(
                    checkpoint_path.is_file()
                    and hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() == checkpoint_digest
                    and _generation_source_fingerprint() == source_fingerprint
                    and _current_projection_fingerprint(current) == projection_fingerprint
                    and current["fence"] == projection["fence"]
                    and hashlib.sha256(mihomo_path.read_bytes()).hexdigest() == mihomo_hash
                    and get_mihomo_runtime_incarnation() == mihomo_incarnation
                    and _current_bindings_artifact_parity(bindings, modes) == (True, bindings_digest)
                    and all(DEFAULT_MIHOMO_ADAPTER.check_port(
                        int(item.get("port") or 0), host="172.18.0.1", timeout=1.0)
                        for item in handoffs)
                    and hashlib.sha256(config_path.read_bytes()).hexdigest() == (candidate_sha if applied else before_config_hash)
                    and adapter.get_runtime_config_sha256() == (candidate_sha if applied else before_mounted_hash)
                    and (runtime_incarnation is None or adapter.get_runtime_incarnation() == runtime_incarnation)
                )
            except Exception:
                return False

        def core_selection_stable() -> bool:
            try:
                selection_proof = _verify_generation_recovery_selection_readback(selection)
                if not selection_proof.get("ok"):
                    return False
                if selection_proof.get("mode") == "direct":
                    return True
                if str((selection.get("routing") or {}).get("server_mode") or "auto").lower() == "auto":
                    from fwrouter_api.services.selector import get_vpn_auto_state
                    auto_state = get_vpn_auto_state(read_only=True)
                    return bool(
                        auto_state.get("active_auto_target_valid") is True
                        and auto_state.get("config_consistent") is True
                        and str(auto_state.get("active_auto_server_id") or "")
                        == str((selection.get("routing") or {}).get("active_auto_server_id") or "")
                    )
                return True
            except Exception:
                return False

        if not stable(runtime_incarnation=before_incarnation) or not core_selection_stable():
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "preapply_revalidation_failed"}
        if supersession_owned:
            after_incarnation = before_incarnation
        elif resume_owned_restart:
            after_incarnation = before_incarnation
        else:
            save_attempt("reload_started")
            atomic_write_text(config_path, candidate_text)
            reload_result = adapter.reload()
            if not reload_result.ok:
                return {"ok": False, "recovered": "current_projection_unproven", "reason": "xray_reload_failed"}
            after_incarnation = adapter.get_runtime_incarnation()
        if (adapter.get_runtime_config_sha256() != candidate_sha
                or (not resume_owned_restart and not supersession_owned
                    and after_incarnation == before_incarnation)
                or not stable(applied=True, runtime_incarnation=after_incarnation)):
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "runtime_revalidation_failed"}
        loaded = adapter.list_loaded_client_identities()
        if sorted(loaded) != expected:
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "loaded_identity_mismatch"}
        bindings_readback = _verify_active_config_bindings(bindings, expected_client_identities=expected)
        modes_readback = _verify_active_config_client_modes(modes)
        selection_readback = _verify_generation_recovery_selection_readback(selection)
        if not bindings_readback.get("ok") or not modes_readback.get("ok") or not selection_readback.get("ok"):
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "runtime_projection_readback_failed"}
        if not core_selection_stable():
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "auto_target_unverified"}
        if not stable(applied=True, runtime_incarnation=after_incarnation):
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "preterminal_revalidation_failed"}

        save_attempt("superseded_verified", verified_at=time.time(),
                     observed_runtime_incarnation=after_incarnation,
                     loaded_identity_count=len(loaded),
                     bindings_verified=int(bindings_readback.get("verified_bindings_count") or 0),
                     modes_verified=int(modes_readback.get("verified_client_modes_count") or 0),
                     selection_mode=selection_readback.get("mode"),
                     selection_target=selection_readback.get("logical_server_id"))
        if not stable(applied=True, runtime_incarnation=after_incarnation) or not core_selection_stable():
            return {"ok": False, "recovered": "current_projection_unproven", "reason": "terminal_revalidation_failed"}
        # A checkpoint-bound recovery may finish a refresh that crashed after
        # native apply. Close the durable admission only after rebuilding the
        # native receipt from current readback; never infer verification from
        # the checkpoint fingerprint alone.
        from fwrouter_api.services.subscription import (
            _claim_subscription_inventory_admission,
            _promote_subscription_inventory_generation,
            get_subscription_native_generation_state,
        )
        from fwrouter_api.services.mihomo_reconcile import _native_udp_policy_proof
        from fwrouter_api.services.mihomo_reconcile_fingerprint import (
            current_mihomo_input_fingerprint,
            write_mihomo_reconcile_fingerprint_state,
        )

        if inventory_state.get("status") in {"pending", "verified"}:
            admission = None
            if inventory_state.get("status") == "pending":
                admission = _claim_subscription_inventory_admission()
                if (
                    not isinstance(inventory_binding, dict)
                    or inventory_binding.get("digest") != admission.digest
                    or inventory_binding.get("revision") != admission.revision
                ):
                    return {"ok": False, "recovered": "current_projection_unproven",
                            "reason": "inventory_checkpoint_binding_mismatch"}

            def inventory_generation_stable() -> bool:
                current = get_subscription_native_generation_state()
                return bool(
                    current.get("status") == inventory_state.get("status")
                    and current.get("candidate_digest") == inventory_state.get("candidate_digest")
                    and current.get("candidate_revision") == inventory_state.get("candidate_revision")
                    and isinstance(inventory_binding, dict)
                    and inventory_binding.get("digest") == current.get("candidate_digest")
                    and inventory_binding.get("revision") == current.get("candidate_revision")
                )

            try:
                mihomo_fingerprint = current_mihomo_input_fingerprint()
                native_proof = _native_udp_policy_proof()
                active_mihomo_hash = hashlib.sha256(mihomo_path.read_bytes()).hexdigest()
                receipt_written = bool(
                    native_proof.get("ok")
                    and stable(applied=True, runtime_incarnation=after_incarnation)
                    and core_selection_stable()
                    and inventory_generation_stable()
                    and get_mihomo_runtime_incarnation() == mihomo_incarnation
                    and write_mihomo_reconcile_fingerprint_state(
                        fingerprint=mihomo_fingerprint,
                        result={"ok": True, "reconcile_reason": "xray_checkpoint_recovery"},
                        native_proof=native_proof,
                        expected_active_hash=active_mihomo_hash,
                        expected_runtime_incarnation=mihomo_incarnation,
                    )
                    and inventory_generation_stable()
                    and stable(applied=True, runtime_incarnation=after_incarnation)
                    and core_selection_stable()
                )
            except Exception:
                receipt_written = False
            if not receipt_written:
                return {"ok": False, "recovered": "current_projection_unproven",
                        "reason": "inventory_native_receipt_unconfirmed"}
            if admission is not None:
                promoted_inventory = _promote_subscription_inventory_generation(admission)
                if not promoted_inventory.get("ok"):
                    return {"ok": False, "recovered": "current_projection_unproven",
                            "reason": "inventory_generation_promotion_unconfirmed"}
                inventory_state = get_subscription_native_generation_state()
                if (inventory_state.get("status") != "verified"
                        or inventory_state.get("candidate_digest") != admission.digest
                        or inventory_state.get("candidate_revision") != admission.revision):
                    return {"ok": False, "recovered": "current_projection_unproven",
                            "reason": "inventory_generation_promotion_unconfirmed"}
        elif inventory_state.get("status") != "empty":
            return {"ok": False, "recovered": "current_projection_unproven",
                    "reason": "inventory_generation_unverified"}
        checkpoint_path.unlink()
        _fsync_directory(checkpoint_path.parent)
        return {"ok": True, "recovered": "current_projection_verified", "operation_id": attempt["operation_id"],
                "loaded_identity_count": len(loaded), "bindings_verified": bindings_readback.get("verified_bindings_count"),
                "modes_verified": modes_readback.get("verified_client_modes_count")}
    except Exception as exc:
        # Keep exception messages/native payloads private; only return stable type/code.
        code = getattr(exc, "code", None)
        reason = str(code) if isinstance(code, str) and code.startswith("XRAY_") else type(exc).__name__
        return {"ok": False, "recovered": "current_projection_unproven", "reason": reason}


def _mark_generation_selection_verification_required(checkpoint_path: Path) -> None:
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    data["selection_verification_required"] = True
    _atomic_write_private_checkpoint(checkpoint_path, json.dumps(data, sort_keys=True))


def _record_xray_generation_derived_rows(
    checkpoint_path: Path, *, phase: str,
    expected_generation_id: str | None = None,
    expected_source_fingerprint: str | None = None,
) -> None:
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if expected_generation_id is not None or expected_source_fingerprint is not None:
        if (
            not expected_generation_id
            or not expected_source_fingerprint
            or data.get("generation_id") != expected_generation_id
            or data.get("derived_source_fingerprint") != expected_source_fingerprint
            or _generation_source_fingerprint() != expected_source_fingerprint
        ):
            raise ValueError("XRAY_GENERATION_DERIVED_ROW_OWNER_CHANGED")
    auto_selection_after = _capture_generation_auto_selection()
    derived_rows_after = _capture_generation_derived_rows(
        managed_email_prefixes=list(data.get("managed_email_prefixes") or []),
        expected_client_identities=list(data.get("expected_client_identities") or []),
    )
    if expected_source_fingerprint is not None and (
        data.get("generation_id") != expected_generation_id
        or data.get("derived_source_fingerprint") != expected_source_fingerprint
        or _generation_source_fingerprint() != expected_source_fingerprint
    ):
        raise ValueError("XRAY_GENERATION_DERIVED_ROW_OWNER_CHANGED")
    data["auto_selection_after"] = auto_selection_after
    data["derived_rows_after"] = derived_rows_after
    # The owner CAS above proves this is still the same source snapshot. Do
    # not replace it with a fresh fingerprint that could adopt foreign writes.
    data["derived_source_fingerprint"] = (
        expected_source_fingerprint
        if expected_source_fingerprint is not None
        else _generation_source_fingerprint()
    )
    data["phase"] = phase
    _atomic_write_private_checkpoint(checkpoint_path, json.dumps(data, sort_keys=True))


def _xray_generation_snapshots_changed(checkpoint_path: Path) -> bool:
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    previous = data.get("subscription_snapshots") if isinstance(data.get("subscription_snapshots"), dict) else {}
    with db_session() as connection:
        for token, old in previous.items():
            row = connection.execute(
                "SELECT nodes_json FROM subscription_profile_snapshots WHERE token = ?",
                (token,),
            ).fetchone()
            old_nodes = old.get("nodes_json") if isinstance(old, dict) else None
            new_nodes = str(row["nodes_json"]) if row else None
            if old_nodes != new_nodes:
                return True
    return False


def _record_xray_generation_snapshot_postimage(checkpoint_path: Path) -> None:

    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    tokens = list((data.get("subscription_snapshots") or {}).keys())
    after: dict[str, dict[str, Any] | None] = {}
    with db_session() as connection:
        for token in tokens:
            row = connection.execute(
                "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots WHERE token = ?",
                (token,),
            ).fetchone()
            after[token] = dict(row) if row else None
    data["subscription_snapshots_after"] = after
    data["phase"] = "snapshots_published"
    _atomic_write_private_checkpoint(checkpoint_path, json.dumps(data, sort_keys=True))


def _ordinary_fixed_checkpoint_context_matches(
    checkpoint: dict[str, Any], *, expected_source: str, expected_revision: int,
) -> bool:
    """Revalidate the checkpoint's original ordinary-fixed Core/routing owner."""
    route_snapshot = checkpoint.get("fixed_routing_snapshot")
    if route_snapshot is None:
        return True
    if not isinstance(route_snapshot, dict):
        return False
    fixed_workflow_revision = checkpoint.get("fixed_workflow_revision")
    if type(fixed_workflow_revision) is int:
        # Fixed-workflow publication is fenced by its existing callback and
        # workflow receipt; this helper adds proof only for ordinary fixed
        # startup checkpoints.
        return True
    selection_snapshot = checkpoint.get("auto_selection_after")
    if not isinstance(selection_snapshot, dict):
        selection_snapshot = checkpoint.get("auto_selection_before")
    core_proof = checkpoint.get("ordinary_fixed_core_proof")
    core_runtime_fingerprint = checkpoint.get("ordinary_fixed_core_runtime_fingerprint")
    if (
        not isinstance(selection_snapshot, dict)
        or selection_snapshot.get("selection_revision") != expected_revision
        or checkpoint.get("selection_revision") != expected_revision
        or not isinstance(core_proof, dict)
        or not isinstance(core_runtime_fingerprint, str)
        or len(core_runtime_fingerprint) != 64
        or not isinstance(expected_source, str)
        or len(expected_source) != 64
    ):
        return False
    context = {
        "routing_snapshot": route_snapshot,
        "selection_snapshot": selection_snapshot,
        "core_proof": core_proof,
        "core_runtime_fingerprint": core_runtime_fingerprint,
        "source_fingerprint": expected_source,
    }
    return _fixed_xray_generation_context_matches(
        route_snapshot, None,
        expected_selection_revision=expected_revision,
        ordinary_fixed_context=context,
    )


def _publish_mihomo_udp_receipt_with_owner(
    *, expected_mihomo_runtime: str, expected_mihomo_sha: str,
    reconcile_reason: str, operation_id: str, owner_matches: Any,
    routing_snapshot: dict[str, Any] | None = None,
) -> bool:
    """Share native proof and durable receipt persistence across fenced owners."""
    from fwrouter_api.services.mihomo_reconcile import _native_udp_policy_proof
    from fwrouter_api.services.mihomo_reconcile_fingerprint import (
        current_mihomo_input_fingerprint,
        write_mihomo_reconcile_fingerprint_state,
    )

    try:
        if not xray_writer_guard_is_held() or not callable(owner_matches):
            return False
        fingerprint = (
            current_mihomo_input_fingerprint(routing_snapshot)
            if isinstance(routing_snapshot, dict)
            else current_mihomo_input_fingerprint()
        )
        fingerprint_hash = str(fingerprint.get("hash") or "")
        if not fingerprint_hash or not owner_matches(fingerprint_hash):
            return False
        native_proof = _native_udp_policy_proof()
        if not native_proof.get("ok") or not owner_matches(fingerprint_hash):
            return False
        if not (
            native_proof.get("native_top_level_rules_match") is True
            and native_proof.get("selector_membership_matches") is True
            and native_proof.get("guarded_rules_match") is True
        ):
            return False
        return bool(write_mihomo_reconcile_fingerprint_state(
            fingerprint=fingerprint,
            result={"ok": True, "reconcile_reason": reconcile_reason, "operation_id": operation_id},
            native_proof=native_proof,
            expected_active_hash=expected_mihomo_sha,
            expected_runtime_incarnation=expected_mihomo_runtime,
        ))
    except Exception:
        return False


def _publish_xray_generation_mihomo_udp_receipt(
    *, checkpoint_path: Path, verification: dict[str, Any], expected_source: str,
    expected_revision: int, expected_mihomo_runtime: str, expected_mihomo_sha: str,
) -> bool:
    """Publish a Mihomo receipt only while this staged generation still owns runtime state."""
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    try:
        if not xray_writer_guard_is_held():
            return False
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        verification_required = checkpoint.get("selection_verification_required") is True
        if verification_required and verification.get("ok") is not True:
            return False
        if not verification_required and "ok" in verification and verification.get("ok") is not True:
            return False
        generation_id = str(checkpoint.get("generation_id") or "")
        routing_snapshot = checkpoint.get("fixed_routing_snapshot")
        if not isinstance(routing_snapshot, dict):
            routing_snapshot = None
        if (
            not generation_id
            or not expected_source
            or not expected_mihomo_runtime
            or not expected_mihomo_sha
            or checkpoint.get("selection_revision") != expected_revision
            or checkpoint.get("mihomo_runtime_incarnation") != expected_mihomo_runtime
            or checkpoint.get("derived_source_fingerprint") != expected_source
            or (checkpoint.get("staged_generation") or {}).get("mihomo_final_sha256") != expected_mihomo_sha
        ):
            return False
        if not _ordinary_fixed_checkpoint_context_matches(
                checkpoint, expected_source=expected_source,
                expected_revision=expected_revision):
            return False
        verification_operation_id = verification.get("operation_id")
        if checkpoint.get("selection_verification_required") is True:
            if verification_operation_id != generation_id or verification.get("selection_revision") != expected_revision:
                return False
        elif verification_operation_id is not None and verification_operation_id != generation_id:
            return False
        elif verification.get("selection_revision") is not None and verification.get("selection_revision") != expected_revision:
            return False

        def owned(fingerprint_hash: str) -> bool:
            try:
                current_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                with db_session() as connection:
                    fence = read_selection_fence(connection)
                active_hash = hashlib.sha256(
                    Path(mihomo_config._resolved_base_config_path()).read_bytes()
                ).hexdigest()
                current_fingerprint = (
                    current_mihomo_input_fingerprint(routing_snapshot)
                    if isinstance(routing_snapshot, dict)
                    else current_mihomo_input_fingerprint()
                )
                return bool(
                    current_checkpoint.get("generation_id") == generation_id
                    and current_checkpoint.get("selection_revision") == expected_revision
                    and current_checkpoint.get("mihomo_runtime_incarnation") == expected_mihomo_runtime
                    and current_checkpoint.get("derived_source_fingerprint") == expected_source
                    and (current_checkpoint.get("staged_generation") or {}).get("mihomo_final_sha256") == expected_mihomo_sha
                    and fence.get("revision") == expected_revision
                    and _generation_source_fingerprint() == expected_source
                    and get_mihomo_runtime_incarnation() == expected_mihomo_runtime
                    and active_hash == expected_mihomo_sha
                    and current_fingerprint.get("hash") == fingerprint_hash
                    and _ordinary_fixed_checkpoint_context_matches(
                        current_checkpoint, expected_source=expected_source,
                        expected_revision=expected_revision,
                    )
                )
            except (OSError, ValueError, TypeError):
                return False

        return _publish_mihomo_udp_receipt_with_owner(
            expected_mihomo_runtime=expected_mihomo_runtime,
            expected_mihomo_sha=expected_mihomo_sha,
            reconcile_reason="xray_staged_generation",
            operation_id=generation_id,
            owner_matches=owned,
            routing_snapshot=routing_snapshot,
        )
    except Exception:
        return False


def _publish_xray_generation_rollback_mihomo_udp_receipt(
    *, checkpoint_path: Path, expected_source: str, expected_revision: int,
    expected_mihomo_runtime: str, expected_mihomo_sha: str,
) -> bool:
    """Re-prove the restored last-good Mihomo instance before accepting rollback."""
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.mihomo_reconcile_fingerprint import current_mihomo_input_fingerprint
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    try:
        if not xray_writer_guard_is_held() or not all((expected_source, expected_mihomo_runtime, expected_mihomo_sha)):
            return False
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        generation_id = str(checkpoint.get("generation_id") or "")
        routing_snapshot = checkpoint.get("fixed_routing_snapshot")
        if not isinstance(routing_snapshot, dict):
            routing_snapshot = None
        def checkpoint_preimage_matches(value: dict[str, Any]) -> bool:
            try:
                artifacts = value.get("artifacts") if isinstance(value.get("artifacts"), dict) else {}
                mihomo_artifact = artifacts.get("mihomo_config") if isinstance(artifacts.get("mihomo_config"), dict) else {}
                encoded = mihomo_artifact.get("text")
                return bool(
                    isinstance(encoded, str)
                    and hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest() == expected_mihomo_sha
                )
            except (ValueError, TypeError):
                return False

        if (
            not generation_id
            or type(expected_revision) is not int
            or checkpoint.get("phase") != "restore_started"
            or checkpoint.get("source_fingerprint") != expected_source
            or not checkpoint_preimage_matches(checkpoint)
        ):
            return False

        def owned(fingerprint_hash: str) -> bool:
            try:
                current_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                with db_session() as connection:
                    fence = read_selection_fence(connection)
                active_hash = hashlib.sha256(
                    Path(mihomo_config._resolved_base_config_path()).read_bytes()
                ).hexdigest()
                current_fingerprint = (
                    current_mihomo_input_fingerprint(routing_snapshot)
                    if isinstance(routing_snapshot, dict)
                    else current_mihomo_input_fingerprint()
                )
                return bool(
                    current_checkpoint.get("generation_id") == generation_id
                    and current_checkpoint.get("phase") == "restore_started"
                    and current_checkpoint.get("source_fingerprint") == expected_source
                    and checkpoint_preimage_matches(current_checkpoint)
                    and fence.get("revision") == expected_revision
                    and _generation_source_fingerprint() == expected_source
                    and get_mihomo_runtime_incarnation() == expected_mihomo_runtime
                    and active_hash == expected_mihomo_sha
                    and current_fingerprint.get("hash") == fingerprint_hash
                )
            except (OSError, ValueError, TypeError):
                return False

        return _publish_mihomo_udp_receipt_with_owner(
            expected_mihomo_runtime=expected_mihomo_runtime,
            expected_mihomo_sha=expected_mihomo_sha,
            reconcile_reason="xray_generation_rollback",
            operation_id=f"{generation_id}:rollback",
            owner_matches=owned,
            routing_snapshot=routing_snapshot,
        )
    except Exception:
        return False


def _finish_xray_generation_rollback(
    checkpoint_path: Path, *, restored: bool,
) -> bool:
    """Close only a fully re-proved rollback; retain and mark all incomplete recovery."""
    if restored:
        checkpoint_path.unlink(missing_ok=True)
        _fsync_directory(checkpoint_path.parent)
        return True
    _update_xray_generation_checkpoint(checkpoint_path, phase="restore_failed")
    return False


def _finalize_xray_profile_publication(pending: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    """Publish only after the caller's out-of-guard Core verification succeeded."""
    values = pending.get("result_values") or {}
    checkpoint_path = Path(pending["checkpoint_path"])
    staged = values.get("staged_generation") if isinstance(values.get("staged_generation"), dict) else {}
    native_xray = ((staged.get("native_validation") or {}).get("xray") or {})
    expected_identities = sorted(
        (str(pair[0]), str(pair[1])) for pair in native_xray.get("expected_client_identities", [])
    )
    adapter = pending.get("adapter")
    checkpoint: dict[str, Any] = {}

    def pending_failure(stage: str, error_code: str, **details: Any) -> dict[str, Any]:
        response = {
            "ok": False, "status": "pending", "stage": stage,
            "error_code": error_code, **details,
        }
        ordinary_fixed_checkpoint = bool(
            isinstance(checkpoint.get("fixed_routing_snapshot"), dict)
            and type(checkpoint.get("fixed_workflow_revision")) is not int
        )
        if ordinary_fixed_checkpoint:
            # The checkpoint is still the operation owner, but it does not
            # prove that the runtime was restored to its last-good state.
            response["runtime_verified"] = False
        else:
            # Preserve the established auto/legacy and pending-fixed
            # response contract; this correction is ordinary-fixed only.
            response["last_good_retained"] = True
        return response

    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        expected_runtime = str(checkpoint.get("xray_runtime_incarnation_after") or "")
        expected_sha = str(staged.get("xray_candidate_sha256") or "")
        expected_mihomo_runtime = str(checkpoint.get("mihomo_runtime_incarnation") or "")
        expected_mihomo_sha = str(
            checkpoint.get("staged_generation", {}).get("mihomo_final_sha256") or ""
        )
        expected_source = str(checkpoint.get("derived_source_fingerprint") or "")
        expected_revision = checkpoint.get("selection_revision")
        expected_xray_runtime = str(checkpoint.get("xray_runtime_incarnation_after") or "")
        expected_xray_sha = str(staged.get("xray_candidate_sha256") or "")
        fixed_routing_snapshot = checkpoint.get("fixed_routing_snapshot")
        if not isinstance(fixed_routing_snapshot, dict):
            fixed_routing_snapshot = None
        from fwrouter_api.services import mihomo_config
        from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

        def publication_context_matches() -> bool:
            try:
                current_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                xray_path = Path(adapter.config_path)
                xray_file_hash = hashlib.sha256(xray_path.read_bytes()).hexdigest()
                with db_session() as connection:
                    fence = read_selection_fence(connection)
                return bool(
                    current_checkpoint.get("generation_id") == checkpoint.get("generation_id")
                    and current_checkpoint.get("selection_revision") == expected_revision
                    and current_checkpoint.get("mihomo_runtime_incarnation") == expected_mihomo_runtime
                    and current_checkpoint.get("derived_source_fingerprint") == expected_source
                    and (current_checkpoint.get("staged_generation") or {}).get("mihomo_final_sha256") == expected_mihomo_sha
                    and current_checkpoint.get("fixed_routing_snapshot") == checkpoint.get("fixed_routing_snapshot")
                    and current_checkpoint.get("ordinary_fixed_core_proof") == checkpoint.get("ordinary_fixed_core_proof")
                    and current_checkpoint.get("ordinary_fixed_core_runtime_fingerprint")
                        == checkpoint.get("ordinary_fixed_core_runtime_fingerprint")
                    and expected_source
                    and _generation_source_fingerprint() == expected_source
                    and type(expected_revision) is int
                    and fence.get("revision") == expected_revision
                    and (verification.get("selection_revision") in (None, expected_revision))
                    and expected_xray_runtime
                    and adapter.get_runtime_incarnation() == expected_xray_runtime
                    and xray_file_hash == expected_xray_sha
                    and adapter.get_runtime_config_sha256() == expected_xray_sha
                    and sorted(adapter.list_loaded_client_identities()) == expected_identities
                    and expected_mihomo_runtime
                    and get_mihomo_runtime_incarnation() == expected_mihomo_runtime
                    and expected_mihomo_sha
                    and hashlib.sha256(Path(mihomo_config._resolved_base_config_path()).read_bytes()).hexdigest()
                    == expected_mihomo_sha
                    and _ordinary_fixed_checkpoint_context_matches(
                        current_checkpoint, expected_source=expected_source,
                        expected_revision=expected_revision,
                    )
                )
            except Exception:
                return False

        config_path = Path(adapter.config_path)
        first_incarnation = adapter.get_runtime_incarnation()
        first_config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
        first_mounted_hash = adapter.get_runtime_config_sha256()
        loaded_identities = sorted(adapter.list_loaded_client_identities())
        second_incarnation = adapter.get_runtime_incarnation()
        final_config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
        final_mounted_hash = adapter.get_runtime_config_sha256()
        exact_readback = bool(
            expected_runtime and expected_sha
            and first_incarnation == expected_runtime == second_incarnation
            and first_config_hash == expected_sha == final_config_hash
            and first_mounted_hash == expected_sha == final_mounted_hash
            and loaded_identities == expected_identities
            and publication_context_matches()
        )
    except Exception:
        exact_readback = False
    if not exact_readback:
        return pending_failure("xray_runtime_readback", "XRAY_GENERATION_RUNTIME_READBACK_FAILED")
    if type(expected_revision) is not int or not _publish_xray_generation_mihomo_udp_receipt(
        checkpoint_path=checkpoint_path,
        verification=verification,
        expected_source=expected_source,
        expected_revision=expected_revision,
        expected_mihomo_runtime=expected_mihomo_runtime,
        expected_mihomo_sha=expected_mihomo_sha,
    ):
        return pending_failure("mihomo_udp_policy_receipt", "MIHOMO_UDP_POLICY_RECEIPT_UNCONFIRMED")
    if not publication_context_matches():
        return pending_failure("generation_publication_revalidation", "XRAY_GENERATION_STALE_BEFORE_PUBLICATION")
    promoted_profile = (
        promote_runtime_verified_subscription_nodes(
            pending["subscription_nodes"],
            profile_tokens=pending["affected_profile_tokens"],
        )
        if pending["materialize"] and pending["promote_public_profile"]
        else {"profiles_count": 0, "nodes_count": 0}
    )
    if not publication_context_matches():
        return pending_failure("generation_publication_revalidation", "XRAY_GENERATION_STALE_BEFORE_PUBLICATION")
    if checkpoint_path.exists():
        _record_xray_generation_snapshot_postimage(checkpoint_path)
    if not publication_context_matches():
        return pending_failure("generation_publication_revalidation", "XRAY_GENERATION_STALE_BEFORE_PUBLICATION")
    generation_apply = {
        "transition_mihomo": _strip_raw_payload(values["applied_transition"]),
        "xray": _strip_raw_payload(values["applied_xray"].details),
        "final_mihomo": _strip_raw_payload(values["applied_final_mihomo"]),
        "public_snapshots_changed": _xray_generation_snapshots_changed(checkpoint_path),
    }
    desired_nodes = values["desired_nodes"]
    result = {
        "ok": True, "status": "success", "nodes_count": len(desired_nodes),
        "created_count": len(values["created"]), "deleted_count": len(values["deleted"]),
        "recreated_count": len(values["recreated"]), "created": values["created"],
        "deleted": values["deleted"], "recreated": values["recreated"],
        "client_reconcile": _strip_raw_payload(values["reconcile_details"]),
        "nodes": [{"server_id": node["server_id"], "server_name": node["server_name"],
                   "client_uuid": node["client_uuid"], "client_email": node["client_email"]}
                  for node in desired_nodes],
        "materialize": values["materialize_result"], "public_profile_promote": promoted_profile,
        "generation_apply": generation_apply,
        "pre_publication_verification": verification,
    }
    inventory_admission = pending.get("inventory_admission")
    if not publication_context_matches():
        return pending_failure("generation_publication_revalidation", "XRAY_GENERATION_STALE_BEFORE_PUBLICATION")
    if inventory_admission is not None:
        from fwrouter_api.services.subscription import _promote_subscription_inventory_generation
        generation_promotion = _promote_subscription_inventory_generation(inventory_admission)
        if not generation_promotion.get("ok"):
            return pending_failure(
                "inventory_generation_promotion",
                "SUBSCRIPTION_INVENTORY_GENERATION_PROMOTION_UNVERIFIED",
                inventory_generation_promotion=generation_promotion,
            )
        result["inventory_generation_promotion"] = generation_promotion
        if checkpoint_path.exists():
            _record_xray_generation_derived_rows(
                checkpoint_path,
                phase="inventory_generation_promoted",
                expected_generation_id=str(checkpoint.get("generation_id") or ""),
                expected_source_fingerprint=expected_source,
            )
    if not publication_context_matches():
        return pending_failure("generation_publication_revalidation", "XRAY_GENERATION_STALE_BEFORE_PUBLICATION")
    if checkpoint_path.exists():
        checkpoint_path.unlink(missing_ok=True)
        _fsync_directory(checkpoint_path.parent)
        if not checkpoint_path.exists():
            # Startup may use this receipt to avoid repeating the final Mihomo
            # convergence and profile promotion. Emit it only after staged
            # native proof, publication, and durable checkpoint closure.
            result["staged_publication_receipt"] = {
                "schema": 1,
                "staged_native_publication_complete": True,
            }
    return result


def _finalize_nonstaged_profile_publication(pending: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    values = pending.get("result_values") or {}
    promoted = (
        promote_runtime_verified_subscription_nodes(
            pending["subscription_nodes"], profile_tokens=pending["affected_profile_tokens"],
        )
        if pending["materialize"] and pending["promote_public_profile"]
        else {"profiles_count": 0, "nodes_count": 0}
    )
    desired_nodes = values["desired_nodes"]
    return {
        "ok": True, "status": "success", "nodes_count": len(desired_nodes),
        "created_count": len(values["created"]), "deleted_count": len(values["deleted"]),
        "recreated_count": len(values["recreated"]), "created": values["created"],
        "deleted": values["deleted"], "recreated": values["recreated"],
        "client_reconcile": _strip_raw_payload(values["reconcile_details"]),
        "nodes": [{"server_id": node["server_id"], "server_name": node["server_name"],
                   "client_uuid": node["client_uuid"], "client_email": node["client_email"]}
                  for node in desired_nodes],
        "materialize": values["materialize_result"], "public_profile_promote": promoted,
        "pre_publication_verification": verification,
    }
@xray_writer_guarded
def _restore_xray_generation_checkpoint(adapter: Any, checkpoint_path: Path) -> dict[str, Any]:
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.artifacts import atomic_write_text
    from fwrouter_api.services.mihomo_runtime import restart_mihomo_container

    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    mode = _checkpoint_operation_mode(data)
    if mode == "proof_only_legacy_admission":
        return {"ok": False, "recovered": "proof_only_admission_pending",
                "reason": "proof_only_operation_requires_explicit_resume"}
    if type(data.get("fixed_workflow_revision")) is int:
        # Fixed generation checkpoints have no qualified generic rollback
        # proof. Preserve the checkpoint and leave recovery to the fixed owner.
        return {"ok": False, "status": "pending", "runtime_verified": False,
                "recovered": "fixed_generation_recovery_unproven",
                "reason": "fixed_generation_preimage_not_authorized"}
    if isinstance(data.get("fixed_routing_snapshot"), dict):
        # Ordinary fixed startup has no workflow-owned preimage receipt. Do not
        # let generic recovery roll back or adopt current native state.
        return {"ok": False, "status": "pending", "runtime_verified": False,
                "recovered": "fixed_generation_recovery_unproven",
                "reason": "fixed_generation_preimage_not_authorized"}
    supersession = data.get("supersession") if isinstance(data.get("supersession"), dict) else None
    if supersession is not None and not supersession.get("rollback_preimage_projection_matches"):
        # The old native set was not proven equal to the newly committed DB
        # identity projection. Restoring it would falsely certify a stale
        # preimage as last-good; retain the immutable evidence and stay blocked.
        _update_xray_generation_checkpoint(checkpoint_path, phase="restore_failed")
        return {"ok": False, "recovered": "restore_failed",
                "reason": "supersession_preimage_unproven"}
    # Refuse stale recovery before the first file write/reload. The checkpoint
    # must still own both the runtime incarnation and the selection fence.
    expected_incarnation = str(data.get("mihomo_runtime_incarnation") or "")
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    current_incarnation = get_mihomo_runtime_incarnation()
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_fence
    with db_session() as connection:
        current_fence = read_selection_fence(connection)
    after_selection = data.get("auto_selection_after") if isinstance(data.get("auto_selection_after"), dict) else None
    if after_selection is not None:
        after_routing = after_selection.get("routing") if isinstance(after_selection.get("routing"), dict) else {}
        expected_revision = after_selection.get("selection_revision")
        expected_active = str(after_routing.get("active_auto_server_id") or "").strip() or None
        if (
            type(expected_revision) is not int
            or current_fence.get("revision") != expected_revision
            or current_fence.get("active_server_id") != expected_active
            or current_fence.get("decision_id") != after_selection.get("selection_decision_id")
        ):
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "selection_fence_changed"}
    elif type(data.get("selection_revision")) is not int or current_fence.get("revision") != data.get("selection_revision"):
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "selection_fence_changed"}
    if not expected_incarnation or not current_incarnation or expected_incarnation != current_incarnation:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "runtime_incarnation_changed"}
    staged = data.get("staged_generation") if isinstance(data.get("staged_generation"), dict) else {}
    phase = str(data.get("phase") or "")
    mihomo_expected_hash = (
        staged.get("mihomo_final_sha256") if phase in {"runtime_applied", "inventory_synced", "bindings_written", "projections_cleaned", "selection_verified"}
        else None
    )
    if phase in {"transition_applied", "xray_applied"}:
        # New checkpoints retain the validated transition digest explicitly.
        # Older evidence may contain the full validation record, but a missing
        # digest cannot authorize adoption of arbitrary current native config.
        mihomo_expected_hash = staged.get("mihomo_transition_sha256") or (
            (staged.get("native_validation") or {}).get("transition") or {}
        ).get("candidate_sha256")
        if not isinstance(mihomo_expected_hash, str) or len(mihomo_expected_hash) != 64 or any(
            char not in "0123456789abcdef" for char in mihomo_expected_hash
        ):
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "mihomo_transition_digest_unavailable"}
    if mihomo_expected_hash:
        active_path = Path(mihomo_config._resolved_base_config_path())
        actual_hash = hashlib.sha256(active_path.read_bytes()).hexdigest() if active_path.is_file() else None
        if actual_hash != mihomo_expected_hash:
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "active_mihomo_generation_changed"}

    # Own the current Xray side as well as Mihomo before any fence, file, or DB write.
    artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
    # Checkpoint paths are evidence, never authority over physical targets. A
    # malformed/foreign checkpoint must not redirect a rollback write.
    expected_artifact_paths = {
        "xray_config": Path(adapter.config_path),
        "mihomo_config": Path(mihomo_config._resolved_base_config_path()),
        "xray_bindings": Path(_xray_bindings_path()),
    }
    for artifact_key, expected_path in expected_artifact_paths.items():
        artifact = artifacts.get(artifact_key)
        recorded_value = artifact.get("path") if isinstance(artifact, dict) else None
        recorded_path = Path(str(recorded_value)) if isinstance(recorded_value, str) and recorded_value else None
        try:
            if recorded_path is None or recorded_path.resolve(strict=False) != expected_path.resolve(strict=False):
                return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "artifact_target_mismatch"}
        except (OSError, RuntimeError):
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "artifact_target_mismatch"}
    active_mihomo_path = expected_artifact_paths["mihomo_config"]
    try:
        current_mihomo_sha = hashlib.sha256(active_mihomo_path.read_bytes()).hexdigest()
    except OSError:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "active_mihomo_unavailable"}
    mihomo_artifact = artifacts.get("mihomo_config") if isinstance(artifacts.get("mihomo_config"), dict) else {}
    rollback_selection = data.get("auto_selection_before")
    rollback_routing = rollback_selection.get("routing") if isinstance(rollback_selection, dict) else None
    mihomo_preimage_proof = _validate_rollback_mihomo_preimage(
        mihomo_artifact.get("text"), routing_snapshot=rollback_routing,
    )
    if not mihomo_preimage_proof.get("ok"):
        return {
            "ok": False,
            "recovered": "stale_checkpoint_declined",
            "reason": str(mihomo_preimage_proof.get("reason") or "mihomo_preimage_policy_unproven"),
        }
    xray_artifact = artifacts.get("xray_config") if isinstance(artifacts.get("xray_config"), dict) else {}
    xray_before_encoded = xray_artifact.get("text")
    if not isinstance(xray_before_encoded, str):
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_preimage_unavailable"}
    try:
        xray_before_bytes = base64.b64decode(xray_before_encoded, validate=True)
        xray_before_text = xray_before_bytes.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_preimage_invalid"}
    phase = str(data.get("phase") or "")
    staged_xray = data.get("staged_generation") if isinstance(data.get("staged_generation"), dict) else {}
    applied_xray_phases = {"xray_applied", "runtime_applied", "inventory_synced", "bindings_written",
                           "projections_cleaned", "selection_verified", "snapshots_published"}
    expected_current_xray_sha = (
        str(staged_xray.get("xray_candidate_sha256") or "") if phase in applied_xray_phases
        else hashlib.sha256(xray_before_bytes).hexdigest()
    )
    expected_current_xray_incarnation = (
        str(data.get("xray_runtime_incarnation_after") or "") if phase in applied_xray_phases
        else str(data.get("xray_runtime_incarnation_before") or "")
    )
    try:
        current_xray_sha = hashlib.sha256(Path(adapter.config_path).read_bytes()).hexdigest()
        current_mounted_xray_sha = adapter.get_runtime_config_sha256()
        current_xray_incarnation = adapter.get_runtime_incarnation()
        if (not expected_current_xray_sha or not expected_current_xray_incarnation
                or current_xray_sha != expected_current_xray_sha
                or current_mounted_xray_sha != expected_current_xray_sha
                or current_xray_incarnation != expected_current_xray_incarnation):
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_runtime_ownership_changed"}
        expected_current_ids = (
            data.get("expected_client_identities")
            if phase in applied_xray_phases else None
        )
        if expected_current_ids is None:
            expected_current_payload = json.loads(xray_before_text)
            expected_current_ids = [
                (str(client.get("id") or "").strip(), str(client.get("email") or "").strip())
                for inbound in (expected_current_payload.get("inbounds") or [])
                if isinstance(inbound, dict) and str(inbound.get("tag") or "") == "vless-ws"
                for client in ((inbound.get("settings") or {}).get("clients") or [])
                if isinstance(client, dict) and client.get("id") and client.get("email")
            ]
        expected_current_ids = sorted((str(pair[0]), str(pair[1])) for pair in expected_current_ids)
        if sorted(adapter.list_loaded_client_identities()) != expected_current_ids:
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_runtime_identity_changed"}
    except Exception:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_runtime_readback_unavailable"}

    # A restored generation predating HandlerService is safely bootstrapped in its
    # private candidate, then native-tested before any active artifact is changed.
    try:
        restored_payload = json.loads(xray_before_text)
        if not isinstance(restored_payload, dict):
            raise ValueError("invalid_xray_preimage")
        old_api = [item for item in (restored_payload.get("inbounds") or [])
                   if isinstance(item, dict) and str(item.get("tag") or "") == "fwrouter-api"]
        if old_api and (len(old_api) != 1 or old_api[0] != adapter._managed_api_inbound()):
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_preimage_api_unsafe"}
        restored_bindings_state = json.loads(base64.b64decode(
            (artifacts.get("xray_bindings") or {}).get("text") or "e30=",
        ).decode("utf-8"))
        restored_bindings = restored_bindings_state.get("bindings") if isinstance(restored_bindings_state, dict) else None
        restored_modes = restored_bindings_state.get("client_modes") if isinstance(restored_bindings_state, dict) else None
        if not isinstance(restored_bindings, list) or not isinstance(restored_modes, list):
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_preimage_bindings_unavailable"}
        adapter._ensure_runtime_stats(restored_payload)
        adapter._materialize_managed_egress(
            payload=restored_payload, bindings=restored_bindings, client_modes=restored_modes,
            handoff_assignments=(restored_bindings_state.get("handoff_listeners") or []),
        )
        restore_candidate_path = Path(adapter.config_path).with_name("xray-restore-candidate.json")
        from fwrouter_api.adapters.xray_common import _json_dump
        restore_candidate_text = _json_dump(restored_payload)
        atomic_write_text(restore_candidate_path, restore_candidate_text)
        restore_candidate_path.chmod(0o600)
        restore_candidate_sha = hashlib.sha256(restore_candidate_text.encode("utf-8")).hexdigest()
        restore_validation = adapter.test_config(str(restore_candidate_path))
        if not restore_validation.ok or hashlib.sha256(restore_candidate_path.read_bytes()).hexdigest() != restore_candidate_sha:
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_preimage_native_invalid"}
        restore_payload_ids = sorted(
            (str(client.get("id") or "").strip(), str(client.get("email") or "").strip())
            for inbound in (restored_payload.get("inbounds") or [])
            if isinstance(inbound, dict) and str(inbound.get("tag") or "") == "vless-ws"
            for client in ((inbound.get("settings") or {}).get("clients") or [])
            if isinstance(client, dict) and client.get("id") and client.get("email")
        )
    except Exception:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "xray_preimage_invalid"}

    # Validate every DB ownership scope before advancing a fence or writing any artifact.
    snapshots = data.get("subscription_snapshots") if isinstance(data.get("subscription_snapshots"), dict) else {}
    snapshots_after = data.get("subscription_snapshots_after")
    expected_snapshots = snapshots_after if isinstance(snapshots_after, dict) else snapshots
    current_snapshots: dict[str, dict[str, Any] | None] = {}
    with db_session() as connection:
        for token in snapshots:
            row = connection.execute(
                "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots WHERE token = ?",
                (token,),
            ).fetchone()
            current_snapshots[token] = dict(row) if row else None
    if current_snapshots != expected_snapshots:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "subscription_snapshot_changed"}

    derived_rows_after = data.get("derived_rows_after")
    derived_rows_before = data.get("derived_rows_before") if isinstance(data.get("derived_rows_before"), dict) else {}
    managed_prefixes = data.get("managed_email_prefixes") if isinstance(data.get("managed_email_prefixes"), list) else []
    expected_client_identities = data.get("expected_client_identities") if isinstance(data.get("expected_client_identities"), list) else []
    current_fingerprint = _generation_source_fingerprint()
    expected_source_fingerprint = (
        str(data.get("derived_source_fingerprint") or "") if isinstance(derived_rows_after, dict)
        else str(data.get("source_fingerprint") or "")
    )
    if not expected_source_fingerprint or current_fingerprint != expected_source_fingerprint:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "generation_source_changed"}
    if isinstance(derived_rows_after, dict):
        current_derived_rows = _capture_generation_derived_rows(
            managed_email_prefixes=managed_prefixes,
            expected_client_identities=expected_client_identities,
        )
        if current_derived_rows != derived_rows_after:
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "generation_projection_changed"}

    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    expected_selection = after_selection if isinstance(after_selection, dict) else data.get("auto_selection_before")
    current_selection = _capture_generation_auto_selection()
    def selection_semantics(snapshot: Any) -> dict[str, Any]:
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        routing = snapshot.get("routing") if isinstance(snapshot.get("routing"), dict) else {}
        provenance = snapshot.get("provenance") if isinstance(snapshot.get("provenance"), dict) else {}
        return {
            "routing": {key: routing.get(key) for key in (
                "server_mode", "desired_fixed_server_id", "applied_fixed_server_id", "active_auto_server_id",
            )},
            "provenance": provenance.get("value_json"),
        }
    if not isinstance(expected_selection, dict) or selection_semantics(current_selection) != selection_semantics(expected_selection):
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "selection_provenance_changed"}
    if data.get("selection_verification_required"):
        current_selection_readback = _verify_generation_selection_readback(expected_selection)
        if not current_selection_readback.get("ok"):
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "selection_runtime_unconfirmed"}
    # Fence the rollback before touching any active artifact. The selector restore
    # phase adopts this operation's returned revision; it never restores an old one.
    # Revalidate the full ownership proof immediately before the CAS. In
    # particular, never adopt a revision that arrived after the proof above.
    with db_session() as connection:
        pre_cas_fence = read_selection_fence(connection)
    if pre_cas_fence != current_fence:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "selection_fence_changed"}
    if _generation_source_fingerprint() != current_fingerprint:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "generation_source_changed"}
    with db_session() as connection:
        pre_cas_rows = _capture_generation_derived_rows(
            managed_email_prefixes=managed_prefixes,
            expected_client_identities=expected_client_identities,
        )
    if isinstance(derived_rows_after, dict) and pre_cas_rows != current_derived_rows:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "generation_projection_changed"}
    with db_session() as connection:
        pre_cas_snapshots = {}
        for token in snapshots:
            row = connection.execute(
                "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots WHERE token = ?",
                (token,),
            ).fetchone()
            pre_cas_snapshots[token] = dict(row) if row else None
    if pre_cas_snapshots != current_snapshots:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "subscription_snapshot_changed"}
    pre_cas_selection = _capture_generation_auto_selection()
    if selection_semantics(pre_cas_selection) != selection_semantics(current_selection):
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "selection_provenance_changed"}
    try:
        if (hashlib.sha256(Path(adapter.config_path).read_bytes()).hexdigest() != current_xray_sha
                or adapter.get_runtime_config_sha256() != current_mounted_xray_sha
                or adapter.get_runtime_incarnation() != current_xray_incarnation
                or hashlib.sha256(Path(mihomo_config._resolved_base_config_path()).read_bytes()).hexdigest() != current_mihomo_sha
                or get_mihomo_runtime_incarnation() != current_incarnation):
            return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "runtime_ownership_changed"}
    except Exception:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "runtime_readback_unavailable"}
    with db_session() as connection:
        rollback_revision = advance_selection_revision(
            connection, expected_revision=current_fence["revision"],
        )
    if rollback_revision is None:
        return {"ok": False, "recovered": "stale_checkpoint_declined", "reason": "selection_fence_changed"}
    if isinstance(after_selection, dict):
        after_selection["selection_revision"] = rollback_revision
        data["auto_selection_after"] = after_selection
    data["selection_revision"] = rollback_revision
    data["phase"] = "restore_started"
    _atomic_write_private_checkpoint(checkpoint_path, json.dumps(data, sort_keys=True))
    artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
    for key in ("xray_config", "mihomo_config", "xray_bindings"):
        artifact = artifacts.get(key) if isinstance(artifacts.get(key), dict) else {}
        text_value = artifact.get("text")
        target = Path(artifact.get("path") or "")
        if not isinstance(text_value, str):
            target.unlink(missing_ok=True)
            continue
        raw = (restore_candidate_text.encode("utf-8") if key == "xray_config"
               else base64.b64decode(text_value))
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, raw.decode("utf-8"))
    xray_restore = adapter.reload()
    mihomo_restore = restart_mihomo_container(
        action="force_recreate", selection_fenced=True,
        expected_selection_revision=rollback_revision,
    )
    # Physical operations can be slow. Revalidate the owned revision and the
    # exact post-generation DB scope again before restoring any derived rows.
    pre_db_restore_ok = bool(xray_restore.ok and mihomo_restore.get("ok"))
    try:
        with db_session() as connection:
            physical_fence = read_selection_fence(connection)
            physical_snapshots = {}
            for token in snapshots:
                row = connection.execute(
                    "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots WHERE token = ?",
                    (token,),
                ).fetchone()
                physical_snapshots[token] = dict(row) if row else None
        physical_rows = _capture_generation_derived_rows(
            managed_email_prefixes=managed_prefixes,
            expected_client_identities=expected_client_identities,
        ) if isinstance(derived_rows_after, dict) else None
        physical_selection = _capture_generation_auto_selection()
        actual_restore_xray_sha = hashlib.sha256(Path(adapter.config_path).read_bytes()).hexdigest()
        actual_restore_mounted_sha = adapter.get_runtime_config_sha256()
        actual_restore_xray_incarnation = adapter.get_runtime_incarnation()
        actual_restore_ids = sorted(adapter.list_loaded_client_identities())
        restored_mihomo_text = (artifacts.get("mihomo_config") or {}).get("text")
        expected_restore_mihomo_sha = (
            hashlib.sha256(base64.b64decode(restored_mihomo_text)).hexdigest()
            if isinstance(restored_mihomo_text, str) else None
        )
        actual_restore_mihomo_sha = hashlib.sha256(
            Path(mihomo_config._resolved_base_config_path()).read_bytes()
        ).hexdigest()
        actual_restore_mihomo_incarnation = get_mihomo_runtime_incarnation()
        pre_db_restore_ok = bool(
            pre_db_restore_ok
            and physical_fence.get("revision") == rollback_revision
            and _generation_source_fingerprint() == current_fingerprint
            and (not isinstance(derived_rows_after, dict) or physical_rows == derived_rows_after)
            and physical_snapshots == current_snapshots
            and selection_semantics(physical_selection) == selection_semantics(expected_selection)
            and actual_restore_xray_sha == restore_candidate_sha == actual_restore_mounted_sha
            and actual_restore_xray_incarnation != current_xray_incarnation
            and actual_restore_ids == restore_payload_ids
            and expected_restore_mihomo_sha is not None
            and actual_restore_mihomo_sha == expected_restore_mihomo_sha
            and bool(actual_restore_mihomo_incarnation)
            and actual_restore_mihomo_incarnation != current_incarnation
        )
    except Exception:
        pre_db_restore_ok = False
    derived_restore_ok = pre_db_restore_ok
    with db_session() as connection:
        if derived_restore_ok:
            connection.execute("BEGIN IMMEDIATE")
            write_fence = read_selection_fence(connection)
            write_snapshots: dict[str, dict[str, Any] | None] = {}
            for token in snapshots:
                row = connection.execute(
                    "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots WHERE token = ?",
                    (token,),
                ).fetchone()
                write_snapshots[token] = dict(row) if row else None
            write_rows = _capture_generation_derived_rows(
                managed_email_prefixes=managed_prefixes,
                expected_client_identities=expected_client_identities,
            ) if isinstance(derived_rows_after, dict) else None
            derived_restore_ok = bool(
                write_fence.get("revision") == rollback_revision
                and _generation_source_fingerprint() == current_fingerprint
                and write_snapshots == current_snapshots
                and (not isinstance(derived_rows_after, dict) or write_rows == derived_rows_after)
            )
        if derived_restore_ok and isinstance(derived_rows_after, dict):
            derived_restore_ok = _restore_scoped_generation_rows(
                    connection,
                    before=derived_rows_before,
                    after=derived_rows_after,
                )
        if derived_restore_ok:
            for token, row in snapshots.items():
                if row is None:
                    connection.execute("DELETE FROM subscription_profile_snapshots WHERE token = ?", (token,))
                else:
                    connection.execute(
                        """INSERT INTO subscription_profile_snapshots (token, nodes_json, runtime_verified_at, updated_at)
                           VALUES (?, ?, ?, ?) ON CONFLICT(token) DO UPDATE SET
                           nodes_json=excluded.nodes_json, runtime_verified_at=excluded.runtime_verified_at, updated_at=excluded.updated_at""",
                    (token, row["nodes_json"], row["runtime_verified_at"], row["updated_at"]),
                    )
    selection_restore_result: dict[str, Any] = {"ok": derived_restore_ok, "selection_revision": rollback_revision}
    if derived_restore_ok and isinstance(data.get("auto_selection_after"), dict):
        from fwrouter_api.services.selector import restore_auto_selection_snapshot
        selection_restore_result = restore_auto_selection_snapshot(
            before=data.get("auto_selection_before") or {},
            after=data.get("auto_selection_after") or {},
            operation_id=str(data.get("selection_operation_id") or "") or None,
        )
        derived_restore_ok = bool(selection_restore_result.get("ok"))
    selection_readback = (
        _verify_generation_selection_readback(data.get("auto_selection_before"))
        if derived_restore_ok and data.get("selection_verification_required") and isinstance(data.get("auto_selection_before"), dict)
        else {"ok": derived_restore_ok}
    )
    derived_restore_ok = bool(derived_restore_ok and selection_readback.get("ok"))
    owned_selection_revision = selection_restore_result.get("selection_revision")
    xray_runtime_readback: dict[str, Any] = {"ok": False, "reason": "not_checked"}
    mihomo_runtime_readback: dict[str, Any] = {"ok": False, "reason": "not_checked"}
    restored_mihomo_hash: str | None = None
    restored_mihomo_incarnation: str | None = None
    if bool(xray_restore.ok) and bool(mihomo_restore.get("ok")) and derived_restore_ok:
        try:
            from fwrouter_api.services.xray_materialize import (
                _verify_active_config_bindings, _verify_active_config_client_modes,
            )
            restored_config_hash = hashlib.sha256(Path(adapter.config_path).read_bytes()).hexdigest()
            mounted_hash = adapter.get_runtime_config_sha256()
            restored_incarnation = adapter.get_runtime_incarnation()
            loaded_identities = sorted(adapter.list_loaded_client_identities())
            binding_readback = _verify_active_config_bindings(
                restored_bindings, expected_client_identities=restore_payload_ids,
            )
            mode_readback = _verify_active_config_client_modes(restored_modes)
            xray_runtime_readback = {
                "ok": (restored_config_hash == restore_candidate_sha == mounted_hash
                       and restored_incarnation != current_xray_incarnation
                       and loaded_identities == restore_payload_ids
                       and bool(binding_readback.get("ok")) and bool(mode_readback.get("ok"))),
                "loaded_identity_count": len(loaded_identities),
                "bindings_verified": int(binding_readback.get("verified_bindings_count") or 0),
                "modes_verified": int(mode_readback.get("verified_client_modes_count") or 0),
            }
            restored_mihomo_text = (artifacts.get("mihomo_config") or {}).get("text")
            expected_mihomo_hash = (hashlib.sha256(base64.b64decode(restored_mihomo_text)).hexdigest()
                                    if isinstance(restored_mihomo_text, str) else None)
            active_mihomo_hash = hashlib.sha256(Path(mihomo_config._resolved_base_config_path()).read_bytes()).hexdigest()
            mihomo_after_incarnation = get_mihomo_runtime_incarnation()
            restored_mihomo_hash = active_mihomo_hash
            restored_mihomo_incarnation = mihomo_after_incarnation
            from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
            listeners_ready = all(
                DEFAULT_MIHOMO_ADAPTER.check_port(int(item.get("port") or 0), host="172.18.0.1", timeout=1.0)
                for item in (restored_bindings_state.get("handoff_listeners") or [])
                if isinstance(item, dict)
            )
            mihomo_runtime_readback = {
                "ok": (expected_mihomo_hash is not None and active_mihomo_hash == expected_mihomo_hash
                       and bool(mihomo_after_incarnation)
                       and mihomo_after_incarnation != current_incarnation and listeners_ready),
            }
        except Exception as exc:
            xray_runtime_readback = {"ok": False, "reason": type(exc).__name__}
            mihomo_runtime_readback = {"ok": False, "reason": type(exc).__name__}
    ok = (bool(xray_restore.ok) and bool(mihomo_restore.get("ok")) and derived_restore_ok
          and bool(xray_runtime_readback.get("ok")) and bool(mihomo_runtime_readback.get("ok")))
    if ok:
        with db_session() as connection:
            terminal_fence = read_selection_fence(connection)
        ok = bool(
            type(owned_selection_revision) is int
            and terminal_fence.get("revision") == owned_selection_revision
            and _generation_source_fingerprint() == str(data.get("source_fingerprint") or "")
        )
    receipt_error_code = None
    if ok:
        receipt_ok = _publish_xray_generation_rollback_mihomo_udp_receipt(
            checkpoint_path=checkpoint_path,
            expected_source=str(data.get("source_fingerprint") or ""),
            expected_revision=int(owned_selection_revision),
            expected_mihomo_runtime=str(restored_mihomo_incarnation or ""),
            expected_mihomo_sha=str(restored_mihomo_hash or ""),
        )
        if not receipt_ok:
            ok = False
            receipt_error_code = "MIHOMO_UDP_POLICY_ROLLBACK_RECEIPT_UNCONFIRMED"
    ok = _finish_xray_generation_rollback(checkpoint_path, restored=ok)
    return {"ok": ok, "recovered": "restored_last_good" if ok else "restore_failed",
            **({"error_code": receipt_error_code} if receipt_error_code else {}),
            "mihomo_preimage_proof": {
                "ok": True,
                "policy_verified": True,
                "native_config_validated": True,
                "sha256": mihomo_preimage_proof.get("sha256"),
            },
            "xray_reload": xray_restore.ok,
            "mihomo_restart": bool(mihomo_restore.get("ok")), "derived_rows_restored": derived_restore_ok,
            "selection_readback": selection_readback, "xray_runtime_readback": xray_runtime_readback,
            "mihomo_runtime_readback": mihomo_runtime_readback,
            "selection_restore_revision": owned_selection_revision}


def _fixed_workflow_identity_snapshot() -> str | None:
    """Hash durable Xray identity state only when generation needs no provisioning."""
    try:
        from fwrouter_api.services.subscription_node_identity import credentials_for_scope
        from fwrouter_api.services.subscription_profiles import _subscription_servers

        servers = _subscription_servers()
        auto_servers = _vpn_auto_servers_for_xray_subscription()
        with db_session() as connection:
            identity_rows = [dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_identities ORDER BY identity_id",
            )]
            alias_rows = [dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_aliases ORDER BY server_id",
            )]
            credential_rows = [dict(row) for row in connection.execute(
                "SELECT * FROM subscription_node_credentials ORDER BY scope, identity_id",
            )]
            token_rows = connection.execute(
                "SELECT c.token FROM subscription_clients c JOIN subscription_accounts a "
                "ON a.account_id=c.account_id WHERE c.enabled=1 AND a.enabled=1 ORDER BY c.token",
            ).fetchall()
            aliases = {str(row["server_id"]): str(row["identity_id"]) for row in alias_rows}
            identities = {str(row["identity_id"]) for row in identity_rows}
            for server in servers:
                server_id = str(server.get("server_id") or "")
                identity_id = aliases.get(server_id)
                if not server_id or not identity_id or identity_id not in identities:
                    return None
                for token in token_rows:
                    credential = credentials_for_scope(
                        connection, "subscription:" + str(token["token"]),
                    ).get(server_id)
                    if credential is None:
                        return None
            auto_credentials = credentials_for_scope(connection, "vpn-auto")
            for server in auto_servers:
                credential = auto_credentials.get(str(server.get("server_id") or ""))
                if credential is None:
                    return None
        return hashlib.sha256(json.dumps(
            {"identities": identity_rows, "aliases": alias_rows, "credentials": credential_rows},
            sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()
    except Exception:
        return None


def reconcile_xray_vpn_auto_subscription(
    *,
    requested_by: str = "api",
    verification_callback: Any = None,
    _inventory_admission: Any = None,
    _fixed_workflow_revision: int | None = None,
    _fixed_routing_snapshot: dict[str, Any] | None = None,
    _fixed_origin_selection_revision: int | None = None,
) -> dict[str, Any]:
    result = reconcile_xray_subscription_profile_nodes(
        requested_by=requested_by,
        include_vpn_auto=True,
        verification_callback=verification_callback,
        _inventory_admission=_inventory_admission,
        _fixed_workflow_revision=_fixed_workflow_revision,
        _fixed_routing_snapshot=_fixed_routing_snapshot,
        _fixed_origin_selection_revision=_fixed_origin_selection_revision,
    )
    return {**result, "profile_reconcile": result}


def reconcile_xray_subscription_profile_nodes(
    *, requested_by: str = "api", materialize: bool = True,
    token_or_slug: str | None = None, promote_public_profile: bool = True,
    cleanup_deleted_projections: bool = True, preserve_existing_overrides: bool = False,
    include_vpn_auto: bool = False, verification_callback: Any = None,
    _inventory_admission: Any = None,
    _fixed_workflow_revision: int | None = None,
    _fixed_routing_snapshot: dict[str, Any] | None = None,
    _fixed_origin_selection_revision: int | None = None,
    _supersede_checkpoint_sha256: str | None = None,
    _migration_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Run staged generation as guarded apply, unguarded verification, guarded publish."""
    with xray_writer_guard(timeout_seconds=30.0):
        from fwrouter_api.services.xray_vpn_auto_pending import _assert_xray_generation_authorized
        if not _assert_xray_generation_authorized(_fixed_workflow_revision):
            return {
                "ok": False,
                "status": "blocked",
                "stage": "fixed_server_workflow_gate",
                "error_code": "XRAY_GENERATION_BLOCKED_BY_FIXED_WORKFLOW",
                "last_good_retained": True,
            }
        if (_fixed_workflow_revision is not None
                and not _fixed_xray_routing_snapshot_matches(
                _fixed_routing_snapshot, _fixed_workflow_revision,
                expected_selection_revision=_fixed_origin_selection_revision,
                origin_selection_revision=_fixed_origin_selection_revision,
                )):
            return {
                "ok": False, "status": "blocked", "stage": "fixed_routing_snapshot",
                "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE",
                "last_good_retained": True,
            }
        ordinary_fixed_context = None
        if _fixed_workflow_revision is None:
            ordinary_fixed_context, ordinary_fixed_error = _capture_ordinary_fixed_xray_context()
            if ordinary_fixed_error is not None:
                return {
                    "ok": False, "status": "pending", "stage": "fixed_routing_snapshot",
                    "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE",
                    "reason": ordinary_fixed_error, "runtime_verified": False,
                }
            if ordinary_fixed_context is not None:
                _fixed_routing_snapshot = ordinary_fixed_context["routing_snapshot"]
        identity_preimage = None
        if _fixed_workflow_revision is not None:
            identity_preimage = _fixed_workflow_identity_snapshot()
            if identity_preimage is None:
                return {
                    "ok": False, "status": "blocked",
                    "stage": "fixed_workflow_identity_preflight",
                    "error_code": "FIXED_WORKFLOW_IDENTITY_UNAVAILABLE",
                    "last_good_retained": True,
                }
        from fwrouter_api.services import mihomo_config
        result = _reconcile_xray_subscription_profile_nodes_guarded(
            requested_by=requested_by, materialize=materialize,
            token_or_slug=token_or_slug, promote_public_profile=promote_public_profile,
            cleanup_deleted_projections=cleanup_deleted_projections,
            preserve_existing_overrides=preserve_existing_overrides,
            include_vpn_auto=include_vpn_auto,
            verification_callback=verification_callback,
            _inventory_admission=_inventory_admission,
            _supersede_checkpoint_sha256=_supersede_checkpoint_sha256,
            _migration_plan_sha256=_migration_plan_sha256,
            _fixed_workflow_revision=_fixed_workflow_revision,
            _fixed_routing_snapshot=_fixed_routing_snapshot,
            _fixed_origin_selection_revision=_fixed_origin_selection_revision,
            _ordinary_fixed_context=ordinary_fixed_context,
            _defer_selection_verification=callable(verification_callback),
        )
        if identity_preimage is not None:
            identity_postimage = _fixed_workflow_identity_snapshot()
            if identity_postimage != identity_preimage:
                return {
                    "ok": False, "status": "partial",
                    "stage": "fixed_workflow_identity_readback",
                    "error_code": "FIXED_WORKFLOW_IDENTITY_CHANGED",
                    "last_good_retained": True,
                }
    pending = result.get("_pending_generation") if isinstance(result, dict) else None
    if not isinstance(pending, dict):
        return result

    if pending.get("staged_generation") is False:
        callback = pending["verification_callback"]
        operation_id = str(pending["operation_id"])
        expected_revision = pending["selection_revision"]
        from fwrouter_api.services.mihomo_reconcile import _invoke_verification_callback
        try:
            value = _invoke_verification_callback(callback, operation_id=operation_id,
                                                 expected_revision=expected_revision)
            verification = value if isinstance(value, dict) else {"ok": bool(value)}
        except Exception:
            verification = {"ok": False, "error_code": "XRAY_GENERATION_FINAL_READBACK_FAILED",
                            "operation_id": operation_id, "selection_revision": expected_revision}
        with xray_writer_guard(timeout_seconds=30.0):
            from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
            from fwrouter_api.services import mihomo_config
            with db_session() as connection:
                fence = read_selection_fence(connection)
            active_path = Path(mihomo_config._resolved_base_config_path())
            try:
                active_hash = hashlib.sha256(active_path.read_bytes()).hexdigest() if active_path.is_file() else None
            except OSError:
                active_hash = None
            owns_context = bool(
                fence.get("revision") == verification.get("selection_revision")
                and str(verification.get("operation_id") or "") == operation_id
                and _generation_source_fingerprint() == pending["source_fingerprint"]
                and get_mihomo_runtime_incarnation() == pending["runtime_incarnation"]
                and active_hash == pending["mihomo_config_sha256"]
            )
            if not owns_context or not verification.get("ok"):
                return {"ok": False, "status": "partial", "stage": "selection_verification",
                        "error_code": str(verification.get("error_code") or "XRAY_GENERATION_STALE_BEFORE_PUBLICATION"),
                        "pre_publication_verification": verification, "last_good_retained": True}
            return _finalize_nonstaged_profile_publication(pending, verification)

    callback = pending["verification_callback"]
    checkpoint_path = Path(pending["checkpoint_path"])
    from fwrouter_api.services.mihomo_reconcile import _invoke_verification_callback
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"ok": False, "status": "pending", "stage": "generation_verification_deferred",
                "error_code": "XRAY_GENERATION_CHECKPOINT_UNAVAILABLE", "last_good_retained": True}
    operation_id = str(pending.get("checkpoint_generation_id") or "")
    expected_revision = pending.get("checkpoint_selection_revision")
    expected_incarnation = pending.get("checkpoint_runtime_incarnation")
    expected_source = pending.get("checkpoint_source_fingerprint")
    if type(expected_revision) is not int:
        return {"ok": False, "status": "pending", "stage": "generation_publication_revalidation",
                "error_code": "XRAY_GENERATION_CONTEXT_INVALID", "last_good_retained": True}
    if (
        str(checkpoint.get("generation_id") or "") != operation_id
        or checkpoint.get("selection_revision") != expected_revision
        or checkpoint.get("mihomo_runtime_incarnation") != expected_incarnation
        or checkpoint.get("derived_source_fingerprint") != expected_source
    ):
        return {"ok": False, "status": "pending", "stage": "generation_verification_deferred",
                "error_code": "XRAY_GENERATION_SUPERSEDED_BEFORE_PROBE", "last_good_retained": True}
    try:
        callback_value = _invoke_verification_callback(
            callback, operation_id=operation_id, expected_revision=expected_revision,
        )
        verification = callback_value if isinstance(callback_value, dict) else {"ok": bool(callback_value)}
    except Exception:
        verification = {"ok": False, "error_code": "XRAY_GENERATION_FINAL_READBACK_FAILED",
                        "operation_id": operation_id, "selection_revision": expected_revision}

    with xray_writer_guard(timeout_seconds=30.0):
        from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        with db_session() as connection:
            fence = read_selection_fence(connection)
        current_incarnation = get_mihomo_runtime_incarnation()
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {}
        except (OSError, ValueError, TypeError):
            checkpoint = {}
        owns_selection = bool(
            str(verification.get("operation_id") or "") == operation_id
            and type(verification.get("selection_revision")) is int
            and fence.get("revision") == verification.get("selection_revision")
        )
        owns_generation = bool(
            checkpoint
            and checkpoint.get("generation_id") == operation_id
            and checkpoint.get("selection_revision") == expected_revision
            and checkpoint.get("mihomo_runtime_incarnation") == expected_incarnation == current_incarnation
            and checkpoint.get("derived_source_fingerprint") == expected_source
            and _generation_source_fingerprint() == expected_source
        )
        staged = checkpoint.get("staged_generation") if isinstance(checkpoint.get("staged_generation"), dict) else {}
        try:
            active_hash = hashlib.sha256(Path(mihomo_config._resolved_base_config_path()).read_bytes()).hexdigest()
        except OSError:
            active_hash = None
        expected_active_hash = staged.get("mihomo_final_sha256")
        if not owns_generation or not owns_selection or active_hash != expected_active_hash:
            return {"ok": False, "status": "pending", "stage": "generation_publication_revalidation",
                    "error_code": "XRAY_GENERATION_STALE_BEFORE_PUBLICATION",
                    "last_good_retained": True}
        checkpoint["selection_operation_id"] = operation_id
        checkpoint["selection_revision"] = fence["revision"]
        checkpoint["pre_publication_verification"] = verification
        _atomic_write_private_checkpoint(checkpoint_path, json.dumps(checkpoint, sort_keys=True))
        if verification.get("ok"):
            _mark_generation_selection_verification_required(checkpoint_path)
            _record_xray_generation_derived_rows(checkpoint_path, phase="selection_verified")
        else:
            restored = _restore_xray_generation_checkpoint(_xray_adapter(), checkpoint_path)
            return {"ok": False, "status": "failed", "stage": "selection_verification",
                    "error_code": str(verification.get("error_code") or "XRAY_GENERATION_FINAL_READBACK_FAILED"),
                    "pre_publication_verification": verification,
                    "last_good_retained": bool(restored.get("ok")), "generation_recovery": restored}
        return _finalize_xray_profile_publication(pending, verification)


@xray_writer_guarded
def _reconcile_xray_subscription_profile_nodes_guarded(
    *,
    requested_by: str = "api",
    materialize: bool = True,
    token_or_slug: str | None = None,
    promote_public_profile: bool = True,
    cleanup_deleted_projections: bool = True,
    preserve_existing_overrides: bool = False,
    include_vpn_auto: bool = False,
    verification_callback: Any = None,
    _inventory_admission: Any = None,
    _defer_selection_verification: bool = False,
    _supersede_checkpoint_sha256: str | None = None,
    _migration_plan_sha256: str | None = None,
    _fixed_workflow_revision: int | None = None,
    _fixed_routing_snapshot: dict[str, Any] | None = None,
    _fixed_origin_selection_revision: int | None = None,
    _ordinary_fixed_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    adapter = _xray_adapter()
    checkpoint_path = _xray_generation_checkpoint_path(adapter)
    blocked = _xray_managed_runtime_blocked("xray_subscription_profile_reconcile")
    module = _module_state("xray") or {}
    managed_enabled = (
        blocked is None
        and str(module.get("desired_state") or "") == "enabled"
        and str(module.get("lifecycle_mode") or "none") == "managed"
    )
    if not managed_enabled:
        if checkpoint_path.exists():
            return {
                "ok": False,
                "status": "blocked",
                "stage": "lifecycle",
                "error_code": "XRAY_GENERATION_RECOVERY_REQUIRES_MANAGED_RUNTIME",
                "last_good_retained": True,
            }
        return {
            **(blocked or {}),
            "ok": True,
            "status": "skipped",
            "reason": "managed_runtime_required" if blocked is not None else "xray_module_disabled",
            "nodes_count": 0,
        }

    supersession: dict[str, Any] | None = None
    if bool(_supersede_checkpoint_sha256) != bool(_migration_plan_sha256):
        return {"ok": False, "status": "failed", "stage": "generation_supersession_admission",
                "error_code": "XRAY_SUPERSESSION_PROOF_REQUIRED"}
    if _supersede_checkpoint_sha256 is not None:
        for value in (_supersede_checkpoint_sha256, _migration_plan_sha256 or ""):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                return {"ok": False, "status": "failed", "stage": "generation_supersession_admission",
                        "error_code": "XRAY_SUPERSESSION_DIGEST_INVALID"}
        if not checkpoint_path.is_file():
            return {"ok": False, "status": "failed", "stage": "generation_supersession_admission",
                    "error_code": "XRAY_SUPERSESSION_CHECKPOINT_UNAVAILABLE"}
        try:
            if hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() != _supersede_checkpoint_sha256:
                raise RuntimeError("XRAY_SUPERSESSION_CHECKPOINT_STALE")
            if _identity_migration_ledger_matches(
                plan_sha256=str(_migration_plan_sha256), checkpoint_sha256=_supersede_checkpoint_sha256,
            ) <= 0:
                raise RuntimeError("XRAY_SUPERSESSION_MIGRATION_PROOF_MISSING")
            supersession = {"checkpoint_sha256": _supersede_checkpoint_sha256,
                            "plan_sha256": _migration_plan_sha256, "baseline": None}
        except Exception as exc:
            code = str(exc)
            if not code.startswith("XRAY_SUPERSESSION_"):
                code = "XRAY_SUPERSESSION_ADMISSION_FAILED"
            return {"ok": False, "status": "failed", "stage": "generation_supersession_admission",
                    "error_code": code}
    if checkpoint_path.exists() and supersession is None:
        checkpoint_result = _resolve_existing_xray_generation_checkpoint(adapter, checkpoint_path)
        if not checkpoint_result.get("ok"):
            return checkpoint_result

    from fwrouter_api.services.subscription import (
        SubscriptionInventoryGenerationBlocked,
        _claim_subscription_inventory_admission,
        _subscription_inventory_generation_gate,
    )
    if supersession is not None and _inventory_admission is None:
        try:
            # Exact checkpoint and migration-plan proofs were checked above.
            # Only this explicit fresh-generation path may claim the current
            # pending candidate; generic startup/reconcile remains fail-closed.
            _inventory_admission = _claim_subscription_inventory_admission()
        except SubscriptionInventoryGenerationBlocked as exc:
            return exc.result
    inventory_gate = _subscription_inventory_generation_gate(
        _inventory_admission, require_writer_guard=True,
    )
    if not inventory_gate.get("ok"):
        return {
            "ok": False,
            "status": "pending",
            "stage": "inventory_generation_admission",
            "error_code": "SUBSCRIPTION_INVENTORY_GENERATION_UNVERIFIED",
            "reason": inventory_gate.get("reason"),
            "last_good_retained": True,
        }

    if not materialize or not promote_public_profile:
        return {
            "ok": False,
            "status": "failed",
            "stage": "generation_publication_required",
            "error_code": "XRAY_GENERATION_PUBLICATION_REQUIRED",
            "error_message": "A staged Xray generation must verify bindings and publish its runtime-verified profile before commit.",
        }

    if callable(verification_callback) and not callable(getattr(adapter, "stage_subscription_generation", None)):
        return {
            "ok": False, "status": "failed", "stage": "verification_phase_unavailable",
            "error_code": "XRAY_STAGED_VERIFICATION_REQUIRED",
            "error_message": "Selection verification requires the staged generation checkpoint path.",
            "last_good_retained": True,
        }

    existing_clients = list(adapter.list_clients())
    checkpoint_preimage_exists = checkpoint_path.is_file()
    checkpoint_preimage_sha256 = (
        _generation_checkpoint_sha256(checkpoint_path) if checkpoint_preimage_exists else None
    )
    identity_intent_journal = _prepare_durable_profile_credentials(
        include_vpn_auto=include_vpn_auto and token_or_slug is None,
        legacy_native_clients=existing_clients,
    )
    if not isinstance(identity_intent_journal, dict) or any(
        not isinstance(identity_intent_journal.get(key), list)
        or any(not isinstance(row, dict) for row in identity_intent_journal.get(key, []))
        for key in ("identities", "aliases", "credentials")
    ):
        return {
            "ok": False, "status": "pending", "stage": "identity_intent_journal",
            "error_code": "XRAY_IDENTITY_INTENT_JOURNAL_UNAVAILABLE",
            "last_good_retained": True,
        }

    def fail_before_checkpoint(result: dict[str, Any]) -> dict[str, Any]:
        compensation = _discard_unapplied_profile_identity_intent(
            identity_intent_journal,
            checkpoint_path=checkpoint_path,
            checkpoint_preimage_exists=checkpoint_preimage_exists,
            checkpoint_preimage_sha256=checkpoint_preimage_sha256,
            expected_source_fingerprint=source_fingerprint,
        )
        if compensation.get("ok"):
            return result
        return {
            "ok": False,
            "status": "pending",
            "stage": "identity_intent_compensation",
            "error_code": "XRAY_IDENTITY_INTENT_COMPENSATION_UNVERIFIED",
            "failed_operation": _generation_failure_operation(result),
            "compensation_reason": str(compensation.get("reason") or "unverified"),
            "last_good_retained": True,
        }

    try:
        source_fingerprint = _generation_source_fingerprint()
    except Exception:
        source_fingerprint = None
        return fail_before_checkpoint({
            "ok": False, "status": "failed", "stage": "generation_source_fingerprint",
            "error_code": "XRAY_GENERATION_SOURCE_FINGERPRINT_UNAVAILABLE",
        })
    if _ordinary_fixed_context is not None:
        _ordinary_fixed_context["source_fingerprint"] = source_fingerprint
        if not _fixed_xray_generation_context_matches(
                _fixed_routing_snapshot, None,
                expected_selection_revision=_ordinary_fixed_context.get("selection_snapshot", {}).get("selection_revision"),
                ordinary_fixed_context=_ordinary_fixed_context):
            return fail_before_checkpoint({
                "ok": False, "status": "pending", "stage": "fixed_routing_snapshot",
                "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE", "runtime_verified": False,
            })
    if supersession is not None:
        try:
            supersession["baseline"] = _checkpoint_supersession_state(
                adapter, checkpoint_path,
                checkpoint_sha256=str(supersession["checkpoint_sha256"]),
                plan_sha256=str(supersession["plan_sha256"]),
            )
        except Exception as exc:
            code = str(exc)
            if not code.startswith("XRAY_SUPERSESSION_"):
                code = "XRAY_SUPERSESSION_ADMISSION_FAILED"
            return fail_before_checkpoint({"ok": False, "status": "failed",
                                           "stage": "generation_supersession_admission",
                                           "error_code": code})
    try:
        subscription_nodes = list_desired_subscription_xray_clients(token_or_slug)
    except Exception:
        return fail_before_checkpoint({"ok": False, "status": "failed",
                                       "stage": "generation_source_projection",
                                       "error_code": "XRAY_GENERATION_SOURCE_PROJECTION_FAILED"})
    desired_nodes = list(subscription_nodes)
    try:
        affected_profile_tokens = list_subscription_profile_tokens(token_or_slug)
    except Exception:
        return fail_before_checkpoint({"ok": False, "status": "failed",
                                       "stage": "generation_profile_projection",
                                       "error_code": "XRAY_GENERATION_PROFILE_PROJECTION_FAILED"})
    if include_vpn_auto and token_or_slug is None:
        from fwrouter_api.services.subscription_node_identity import credentials_for_scope, IdentityConflict
        try:
            with db_session() as connection:
                retained_auto = credentials_for_scope(connection, "vpn-auto")
            auto_servers = _vpn_auto_servers_for_xray_subscription()
        except Exception:
            return fail_before_checkpoint({"ok": False, "status": "failed",
                                           "stage": "generation_auto_projection",
                                           "error_code": "XRAY_GENERATION_AUTO_PROJECTION_FAILED"})
        try:
            for server in auto_servers:
                server_id = str(server["server_id"])
                credential = retained_auto.get(server_id)
                if credential and credential["revoked"]:
                    continue
                if not credential:
                    raise IdentityConflict("IDENTITY_AUTO_CREDENTIAL_UNAVAILABLE")
                email = str(credential["client_email"])
                client_uuid = str(credential["client_uuid"])
                server_name = str(server.get("server_name") or server_id)
                desired_nodes.append({
                    "server_id": server_id,
                    "server_name": server_name,
                    "client_uuid": client_uuid,
                    "client_email": email,
                    "xray_alias": server_name,
                    "vpn_auto": True,
                })
        except Exception:
            return fail_before_checkpoint({"ok": False, "status": "failed",
                                           "stage": "generation_auto_credentials",
                                           "error_code": "XRAY_GENERATION_AUTO_CREDENTIALS_UNAVAILABLE"})
    try:
        desired_by_email = {
            str(node["client_email"]): node
            for node in desired_nodes
            if str(node.get("client_email") or "").strip()
        }
        token_prefix = ""
        if token_or_slug:
            token_prefix = f"sub-{_stable_digest(str(token_or_slug).strip().lower(), length=10)}-"
        desired_clients = [
            {
                "client_uuid": node["client_uuid"],
                "client_id": node["client_uuid"],
                "email": node["client_email"],
                "alias": node["xray_alias"],
            }
            for node in desired_nodes
        ]
        managed_prefixes = [token_prefix] if token_prefix else ["sub-"]
        if include_vpn_auto and token_or_slug is None:
            managed_prefixes.append("vpn-auto-")
    except Exception:
        return fail_before_checkpoint({"ok": False, "status": "failed",
                                       "stage": "generation_candidate_projection",
                                       "error_code": "XRAY_GENERATION_CANDIDATE_PROJECTION_FAILED"})
    staged_generation: dict[str, Any] | None = None
    if callable(getattr(adapter, "stage_subscription_generation", None)):
        try:
            prospective_bindings, prospective_modes, handoff_assignments = _prospective_profile_bindings(
                desired_nodes,
                token_prefix=token_prefix or "sub-",
                managed_email_prefixes=managed_prefixes,
                preserve_existing_overrides=preserve_existing_overrides,
            )
            staged_generation = _stage_profile_native_candidates(
                adapter=adapter,
                desired_clients=desired_clients,
                managed_email_prefixes=managed_prefixes,
                bindings=prospective_bindings,
                client_modes=prospective_modes,
                assignments=handoff_assignments,
                routing_snapshot=_fixed_routing_snapshot,
                fixed_workflow_revision=_fixed_workflow_revision,
                fixed_origin_selection_revision=_fixed_origin_selection_revision,
                ordinary_fixed_context=_ordinary_fixed_context,
            )
            if not isinstance(staged_generation, dict):
                raise TypeError("XRAY_GENERATION_CANDIDATE_RESULT_INVALID")
        except Exception:
            return fail_before_checkpoint({"ok": False, "status": "failed",
                                           "stage": "native_candidate_validation",
                                           "error_code": "XRAY_GENERATION_CANDIDATE_VALIDATION_FAILED"})
        if not staged_generation.get("ok"):
            return fail_before_checkpoint({
                "ok": False,
                "status": "failed",
                "stage": staged_generation.get("stage") or "native_candidate_validation",
                "error_code": staged_generation.get("error_code") or "XRAY_GENERATION_CANDIDATE_INVALID",
                "details": _strip_raw_payload(staged_generation),
            })
        if not _fixed_xray_generation_context_matches(
                _fixed_routing_snapshot, _fixed_workflow_revision,
                expected_selection_revision=(
                    _fixed_origin_selection_revision if _fixed_workflow_revision is not None
                    else (_ordinary_fixed_context or {}).get("selection_snapshot", {}).get("selection_revision")
                ),
                fixed_origin_selection_revision=_fixed_origin_selection_revision,
                ordinary_fixed_context=_ordinary_fixed_context):
            return fail_before_checkpoint({
                "ok": False, "status": "pending", "stage": "fixed_routing_snapshot",
                "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE", "last_good_retained": True,
            })
        try:
            staged_source_fingerprint = _generation_source_fingerprint()
        except Exception:
            return fail_before_checkpoint({"ok": False, "status": "failed",
                                           "stage": "generation_source_revalidation",
                                           "error_code": "XRAY_GENERATION_SOURCE_FINGERPRINT_UNAVAILABLE"})
        if staged_source_fingerprint != source_fingerprint:
            return fail_before_checkpoint({
                "ok": False,
                "status": "failed",
                "stage": "source_intent_changed",
                "error_code": "XRAY_GENERATION_SOURCE_CHANGED_DURING_STAGE",
            })
        if supersession is not None:
            try:
                supersession_still_owned = _checkpoint_supersession_state_unchanged(
                    adapter, checkpoint_path, supersession["baseline"],
                )
            except Exception:
                supersession_still_owned = False
            if not supersession_still_owned:
                return fail_before_checkpoint({"ok": False, "status": "failed",
                                               "stage": "generation_supersession_revalidation",
                                               "error_code": "XRAY_SUPERSESSION_CONTEXT_STALE",
                                               "last_good_retained": True})
        generation_id = uuid4().hex
        try:
            checkpoint_path = _write_xray_generation_checkpoint(
                adapter=adapter,
                generation_id=generation_id,
                tokens=set(affected_profile_tokens),
                phase="prepared",
                source_fingerprint=source_fingerprint,
                staged_generation=staged_generation,
                managed_email_prefixes=managed_prefixes,
                supersession=supersession,
                inventory_generation=(
                    {"digest": _inventory_admission.digest,
                     "revision": _inventory_admission.revision}
                    if _inventory_admission is not None else _current_inventory_generation_binding()
                ),
                fixed_workflow_revision=_fixed_workflow_revision,
                fixed_origin_selection_revision=_fixed_origin_selection_revision,
                ordinary_fixed_context=_ordinary_fixed_context,
            )
        except Exception as exc:
            return fail_before_checkpoint({
                "ok": False,
                "status": "failed",
                "stage": "checkpoint_prepare",
                "error_code": "XRAY_GENERATION_CHECKPOINT_FAILED",
                "error_message": str(exc),
            })
        # Keep the captured routing projection in the private checkpoint and
        # owner context only. Staged metadata can flow into public result
        # projections after verification.
        staged_generation.pop("fixed_routing_snapshot", None)
        transition = staged_generation["mihomo_candidates"]["transition"]
        fixed_stage_selection_revision = (
            _fixed_origin_selection_revision if _fixed_workflow_revision is not None
            else (_ordinary_fixed_context or {}).get("selection_snapshot", {}).get("selection_revision")
        )
        if not _fixed_xray_generation_context_matches(
                _fixed_routing_snapshot, _fixed_workflow_revision,
                expected_selection_revision=(fixed_stage_selection_revision if _fixed_workflow_revision is not None
                                             else (_ordinary_fixed_context or {}).get("selection_snapshot", {}).get("selection_revision")),
                fixed_origin_selection_revision=_fixed_origin_selection_revision,
                ordinary_fixed_context=_ordinary_fixed_context):
            return {"ok": False, "status": "pending", "stage": "fixed_routing_snapshot",
                    "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE", "runtime_verified": False}
        applied_transition = _apply_staged_mihomo_candidate(
            transition,
            staged_generation["native_validation"]["transition"]["candidate_sha256"],
            inventory_admission=_inventory_admission,
            candidate_input_fingerprint=staged_generation.get("mihomo_input_fingerprint"),
            fixed_routing_snapshot=_fixed_routing_snapshot,
            fixed_workflow_revision=_fixed_workflow_revision,
            fixed_origin_selection_revision=_fixed_origin_selection_revision,
            expected_selection_revision=fixed_stage_selection_revision,
            ordinary_fixed_context=_ordinary_fixed_context,
        )
        transition_selection_revision = applied_transition.get("selection_revision")
        if type(transition_selection_revision) is int:
            _record_xray_generation_selection_revision(checkpoint_path, transition_selection_revision)
        elif _fixed_workflow_revision is not None or _ordinary_fixed_context is not None:
            return {"ok": False, "status": "pending", "stage": "selection_revision_receipt",
                    "error_code": "XRAY_GENERATION_SELECTION_REVISION_UNVERIFIED", "runtime_verified": False}
        if not applied_transition.get("ok"):
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {"ok": False, "status": "pending" if (_fixed_workflow_revision is not None or _ordinary_fixed_context is not None) else "failed",
                    "stage": "mihomo_transition_apply", "error_code": "XRAY_GENERATION_TRANSITION_APPLY_FAILED",
                    "details": _strip_raw_payload(applied_transition),
                    **({"runtime_verified": False, "generation_recovery": restored}
                       if (_fixed_workflow_revision is not None or _ordinary_fixed_context is not None) else {})}
        _update_xray_generation_checkpoint(
            checkpoint_path, phase="transition_applied",
            selection_revision=transition_selection_revision,
        )
        if _fixed_workflow_revision is not None:
            fixed_stage_selection_revision = transition_selection_revision
        if not _fixed_xray_generation_context_matches(
                _fixed_routing_snapshot, _fixed_workflow_revision,
                expected_selection_revision=(fixed_stage_selection_revision if _fixed_workflow_revision is not None
                                             else (_ordinary_fixed_context or {}).get("selection_snapshot", {}).get("selection_revision")),
                fixed_origin_selection_revision=_fixed_origin_selection_revision,
                ordinary_fixed_context=_ordinary_fixed_context):
            return {"ok": False, "status": "pending", "stage": "fixed_routing_snapshot",
                    "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE", "runtime_verified": False}
        applied_xray = adapter.apply_staged_subscription_generation(
            staged_generation["xray_candidate_path"],
            expected_sha256=staged_generation["xray_candidate_sha256"],
        )
        if not applied_xray.ok:
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {"ok": False, "status": "pending" if (_fixed_workflow_revision is not None or _ordinary_fixed_context is not None) else "failed",
                    "stage": "xray_generation_apply", "error_code": applied_xray.error_code or "XRAY_GENERATION_APPLY_FAILED",
                    "details": _strip_raw_payload(applied_xray.details),
                    **({"runtime_verified": False, "generation_recovery": restored}
                       if (_fixed_workflow_revision is not None or _ordinary_fixed_context is not None) else {})}
        _update_xray_generation_checkpoint(
            checkpoint_path, phase="xray_applied",
            xray_runtime_incarnation=adapter.get_runtime_incarnation(),
            xray_mounted_config_sha256=adapter.get_runtime_config_sha256(),
        )
        final_path = staged_generation["mihomo_candidates"]["final"]
        if not _fixed_xray_generation_context_matches(
                _fixed_routing_snapshot, _fixed_workflow_revision,
                expected_selection_revision=(fixed_stage_selection_revision if _fixed_workflow_revision is not None
                                             else (_ordinary_fixed_context or {}).get("selection_snapshot", {}).get("selection_revision")),
                fixed_origin_selection_revision=_fixed_origin_selection_revision,
                ordinary_fixed_context=_ordinary_fixed_context):
            return {"ok": False, "status": "pending", "stage": "fixed_routing_snapshot",
                    "error_code": "FIXED_XRAY_ROUTING_SNAPSHOT_STALE", "runtime_verified": False}
        applied_final_mihomo = _apply_staged_mihomo_candidate(
            final_path,
            staged_generation["native_validation"]["final"]["candidate_sha256"],
            inventory_admission=_inventory_admission,
            candidate_input_fingerprint=staged_generation.get("mihomo_input_fingerprint"),
            fixed_routing_snapshot=_fixed_routing_snapshot,
            fixed_workflow_revision=_fixed_workflow_revision,
            fixed_origin_selection_revision=_fixed_origin_selection_revision,
            expected_selection_revision=fixed_stage_selection_revision,
            ordinary_fixed_context=_ordinary_fixed_context,
        )
        final_selection_revision = applied_final_mihomo.get("selection_revision")
        if type(final_selection_revision) is int:
            _record_xray_generation_selection_revision(checkpoint_path, final_selection_revision)
        elif _fixed_workflow_revision is not None or _ordinary_fixed_context is not None:
            return {"ok": False, "status": "pending", "stage": "selection_revision_receipt",
                    "error_code": "XRAY_GENERATION_SELECTION_REVISION_UNVERIFIED", "runtime_verified": False}
        if not applied_final_mihomo.get("ok"):
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {"ok": False, "status": "pending" if (_fixed_workflow_revision is not None or _ordinary_fixed_context is not None) else "failed",
                    "stage": "mihomo_final_apply", "error_code": "XRAY_GENERATION_FINAL_APPLY_FAILED",
                    "details": _strip_raw_payload(applied_final_mihomo), "generation_recovery": restored,
                    **({"runtime_verified": False} if (_fixed_workflow_revision is not None or _ordinary_fixed_context is not None) else {})}
        _update_xray_generation_checkpoint(
            checkpoint_path, phase="runtime_applied",
            selection_revision=final_selection_revision,
        )
        reconcile_details = {"stage": "staged_generation_applied", "created": [], "deleted": [], "recreated": []}
        existing_by_email = {str(client.email or "").lower(): client for client in existing_clients if str(client.email or "")}
        for email, desired in desired_by_email.items():
            current = existing_by_email.get(email.lower())
            expected_uuid = str(desired.get("client_uuid") or "")
            if current is None or str(current.client_uuid or "") != expected_uuid:
                reconcile_details["created"].append({"client_id": expected_uuid, "client_uuid": expected_uuid, "email": email})
            if current is not None and str(current.client_uuid or "") != expected_uuid:
                reconcile_details["recreated"].append({"email": email, "old_client_uuid": current.client_uuid, "new_client_uuid": expected_uuid})
        desired_emails = {email.lower() for email in desired_by_email}
        for email, current in existing_by_email.items():
            if any(email.startswith(prefix.lower()) for prefix in managed_prefixes) and email not in desired_emails:
                reconcile_details["deleted"].append({"client_id": current.client_id, "client_uuid": current.client_uuid, "email": current.email})
    else:
        reconcile_clients_result = adapter.reconcile_clients(
            desired_clients=desired_clients,
            managed_email_prefixes=managed_prefixes,
        )
        if not reconcile_clients_result.ok:
            return {
                "ok": False,
                "status": "failed",
                "stage": "reconcile_profile_clients",
                "error_code": reconcile_clients_result.error_code or "XRAY_SUB_PROFILE_RECONCILE_CLIENTS_FAILED",
                "error_message": reconcile_clients_result.message,
                "details": _strip_raw_payload(reconcile_clients_result.details),
            }
        reconcile_details = reconcile_clients_result.details or {}
    created = [
        {
            **dict(item),
            "server_id": desired_by_email.get(str(item.get("email") or ""), {}).get("server_id"),
        }
        for item in reconcile_details.get("created", [])
        if isinstance(item, dict)
    ]
    deleted = [
        {
            **dict(item),
            "cleanup": _empty_projection_cleanup(),
        }
        for item in reconcile_details.get("deleted", [])
        if isinstance(item, dict)
    ]
    recreated = [
        dict(item)
        for item in reconcile_details.get("recreated", [])
        if isinstance(item, dict)
    ]

    if staged_generation is not None and _generation_source_fingerprint() != source_fingerprint:
        _restore_xray_generation_checkpoint(adapter, checkpoint_path)
        return {
            "ok": False,
            "status": "failed",
            "stage": "source_intent_changed_before_projection",
            "error_code": "XRAY_GENERATION_SOURCE_CHANGED_BEFORE_PROJECTION",
        }

    _sync_xray_inventory(requested_by)
    if staged_generation is not None and checkpoint_path.exists():
        _record_xray_generation_derived_rows(checkpoint_path, phase="inventory_synced")

    binding_result = _batch_materialize_xray_subject_bindings(
        desired_nodes,
        requested_by=requested_by,
        preserve_existing_overrides=preserve_existing_overrides,
    )
    if not binding_result.get("ok"):
        if staged_generation is not None and checkpoint_path.exists():
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
        else:
            restored = None
        return {**binding_result, "status": "failed", **({"generation_recovery": restored} if restored is not None else {})}
    if staged_generation is not None and checkpoint_path.exists():
        _record_xray_generation_derived_rows(checkpoint_path, phase="bindings_written")

    materialize_result: dict[str, Any] | None = None
    if materialize:
        materialize_result = _materialize_xray_runtime_bindings(
            requested_by=requested_by,
            prepare_mihomo_handoff=False,
            bindings_override=prospective_bindings if staged_generation is not None else None,
            client_modes_override=prospective_modes if staged_generation is not None else None,
            candidate_already_applied=staged_generation is not None,
            expected_client_identities=(
                staged_generation["native_validation"]["xray"].get("expected_client_identities")
                if staged_generation is not None
                else None
            ),
        )
        if not materialize_result.get("ok"):
            if staged_generation is not None and checkpoint_path.exists():
                restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            else:
                restored = None
            return {
                "ok": False,
                "status": "failed",
                "stage": "materialize",
                "error_code": "XRAY_SUB_PROFILE_MATERIALIZE_FAILED",
                "error_message": "Failed to materialize Xray subscription profile bindings.",
                "materialize": materialize_result,
                **({"generation_recovery": restored} if restored is not None else {}),
            }

    if cleanup_deleted_projections:
        for item in deleted:
            item["cleanup"] = cleanup_xray_client_projection(
                str(item.get("client_id") or item.get("client_uuid") or "")
            )
    if staged_generation is not None and checkpoint_path.exists():
        _record_xray_generation_derived_rows(checkpoint_path, phase="projections_cleaned")

    pre_publication_verification = None
    if callable(verification_callback):
        if _defer_selection_verification and staged_generation is not None:
            checkpoint_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint_data["selection_operation_id"] = checkpoint_data.get("generation_id")
            _atomic_write_private_checkpoint(checkpoint_path, json.dumps(checkpoint_data, sort_keys=True))
            return {"_pending_generation": {
                "verification_callback": verification_callback,
                "inventory_admission": _inventory_admission,
                "checkpoint_path": str(checkpoint_path),
                "adapter": adapter,
                "subscription_nodes": subscription_nodes,
                "affected_profile_tokens": affected_profile_tokens,
                "materialize": materialize,
                "promote_public_profile": promote_public_profile,
                "checkpoint_generation_id": checkpoint_data.get("generation_id"),
                "checkpoint_selection_revision": checkpoint_data.get("selection_revision"),
                "checkpoint_runtime_incarnation": checkpoint_data.get("mihomo_runtime_incarnation"),
                "checkpoint_source_fingerprint": checkpoint_data.get("derived_source_fingerprint"),
                "result_values": {
                    "desired_nodes": desired_nodes,
                    "created": created,
                    "deleted": deleted,
                    "recreated": recreated,
                    "reconcile_details": reconcile_details,
                    "materialize_result": materialize_result,
                    "applied_transition": applied_transition,
                    "applied_xray": applied_xray,
                    "applied_final_mihomo": applied_final_mihomo,
                    "staged_generation": staged_generation,
                },
            }}
        if _defer_selection_verification and staged_generation is None:
            from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
            from fwrouter_api.services import mihomo_config
            with db_session() as connection:
                fence = read_selection_fence(connection)
            active_path = Path(mihomo_config._resolved_base_config_path())
            active_hash = hashlib.sha256(active_path.read_bytes()).hexdigest() if active_path.is_file() else None
            return {"_pending_generation": {
                "staged_generation": False,
                "inventory_admission": _inventory_admission,
                "verification_callback": verification_callback,
                "operation_id": uuid4().hex,
                "selection_revision": fence["revision"],
                "runtime_incarnation": get_mihomo_runtime_incarnation(),
                "mihomo_config_sha256": active_hash,
                "source_fingerprint": _generation_source_fingerprint(),
                "subscription_nodes": subscription_nodes,
                "affected_profile_tokens": affected_profile_tokens,
                "materialize": materialize,
                "promote_public_profile": promote_public_profile,
                "result_values": {
                    "desired_nodes": desired_nodes, "created": created, "deleted": deleted,
                    "recreated": recreated, "reconcile_details": reconcile_details,
                    "materialize_result": materialize_result,
                },
            }}
        try:
            callback_result = verification_callback()
            pre_publication_verification = callback_result if isinstance(callback_result, dict) else {"ok": bool(callback_result)}
        except Exception:
            pre_publication_verification = {"ok": False, "error_code": "XRAY_GENERATION_FINAL_READBACK_FAILED"}
        if staged_generation is not None and checkpoint_path.exists():
            _mark_generation_selection_verification_required(checkpoint_path)
            _record_xray_generation_derived_rows(checkpoint_path, phase="selection_verified")
        if not bool(pre_publication_verification.get("ok")):
            restored = (
                _restore_xray_generation_checkpoint(adapter, checkpoint_path)
                if staged_generation is not None and checkpoint_path.exists()
                else {"ok": False, "recovered": "no_generation_checkpoint"}
            )
            return {
                "ok": False,
                "status": "failed",
                "stage": "selection_verification",
                "error_code": str(pre_publication_verification.get("error_code") or "XRAY_GENERATION_FINAL_READBACK_FAILED"),
                "error_message": str(pre_publication_verification.get("error_message") or "The current server selection was not verified after runtime apply."),
                "pre_publication_verification": pre_publication_verification,
                "last_good_retained": bool(restored.get("ok")),
                "generation_recovery": restored,
            }

    if staged_generation is not None:
        if not checkpoint_path.exists():
            return {"ok": False, "status": "pending", "stage": "generation_publication",
                    "error_code": "XRAY_GENERATION_CHECKPOINT_UNAVAILABLE",
                    "last_good_retained": True}
        pending_publication = {
            "checkpoint_path": str(checkpoint_path),
            "adapter": adapter,
            "inventory_admission": _inventory_admission,
            "subscription_nodes": subscription_nodes,
            "affected_profile_tokens": affected_profile_tokens,
            "materialize": materialize,
            "promote_public_profile": promote_public_profile,
            "result_values": {
                "desired_nodes": desired_nodes,
                "created": created,
                "deleted": deleted,
                "recreated": recreated,
                "reconcile_details": reconcile_details,
                "materialize_result": materialize_result,
                "applied_transition": applied_transition,
                "applied_xray": applied_xray,
                "applied_final_mihomo": applied_final_mihomo,
                "staged_generation": staged_generation,
            },
        }
        return _finalize_xray_profile_publication(
            pending_publication,
            pre_publication_verification if isinstance(pre_publication_verification, dict) else {},
        )

    promoted_profile = (
        promote_runtime_verified_subscription_nodes(
            subscription_nodes,
            profile_tokens=affected_profile_tokens,
        )
        if materialize and promote_public_profile
        else {"profiles_count": 0, "nodes_count": 0}
    )
    return {
        "ok": True,
        "status": "success",
        "nodes_count": len(desired_nodes),
        "created_count": len(created),
        "deleted_count": len(deleted),
        "recreated_count": len(recreated),
        "created": created,
        "deleted": deleted,
        "recreated": recreated,
        "client_reconcile": _strip_raw_payload(reconcile_details),
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "client_uuid": node["client_uuid"],
                "client_email": node["client_email"],
            }
            for node in desired_nodes
        ],
        "materialize": materialize_result,
        "public_profile_promote": promoted_profile,
        "generation_apply": None,
        "pre_publication_verification": pre_publication_verification,
    }


def export_subscription_profile_text(
    token_or_slug: str,
    *,
    user_agent: str | None,
    requested_format: str | None,
    public_host: str | None = None,
    public_port: int | None = None,
    public_path: str | None = None,
) -> dict[str, Any]:
    return render_subscription_profile(
        token_or_slug,
        user_agent=user_agent,
        requested_format=requested_format,
        public_host=public_host,
        public_port=public_port,
        public_path=public_path,
    )


def _subscription_delete_account_supported(account_id: int, slug: str) -> bool:
    with db_session() as connection:
        rows = connection.execute(
            "SELECT token FROM subscription_clients WHERE account_id = ? ORDER BY client_id",
            (int(account_id),),
        ).fetchall()
    return len(rows) == 1 and str(rows[0]["token"] or "").strip().lower() == str(slug or "").strip().lower()


@xray_writer_guarded
def delete_xray_subscription_profile(
    token_or_slug: str,
    *,
    account_id: int | None = None,
    requested_by: str = "api",
) -> dict[str, Any]:
    token = str(token_or_slug or "").strip().lower()
    if account_id is not None:
        with db_session() as connection:
            exact = connection.execute(
                "SELECT slug FROM subscription_accounts WHERE account_id = ? LIMIT 1",
                (int(account_id),),
            ).fetchone()
        if exact is None or str(exact["slug"] or "").strip().lower() != token:
            return {"ok": False, "status": "failed", "stage": "resolve_account", "error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND", "error_message": "Subscription profile was not found."}
        if not _subscription_delete_account_supported(int(account_id), token):
            return {"ok": False, "status": "failed", "stage": "validate_account_clients", "error_code": "SUBSCRIPTION_PROFILE_CLIENT_SET_UNSUPPORTED", "error_message": "Subscription profile client set is not supported for deletion."}
    disabled = disable_subscription_identity(token_or_slug, account_id=account_id, requested_by=requested_by)
    if not disabled.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "disable_subscription_profile",
            "error_code": disabled.get("error_code") or "SUBSCRIPTION_PROFILE_DELETE_FAILED",
            "error_message": disabled.get("error_message") or "Subscription profile delete failed.",
            "result": disabled,
        }

    deleted_compat_clients: list[dict[str, Any]] = []
    for client in list(_xray_adapter().list_clients()):
        email = str(client.email or "").strip().lower()
        if email not in {token, f"{token}@fwrouter.local"}:
            continue
        result = _xray_adapter().delete_client(client.client_id or client.client_uuid)
        if not result.ok:
            write_operational_log(
                event_type="external_client.delete_failed",
                level="warning",
                subject_id=None,
                message="External client delete failed.",
                details={
                    "subscription_ref": "sub-profile:" + hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "requested_by": safe_actor_identifier(requested_by),
                    "stage": "delete_compatibility_client",
                    "error_code": result.error_code or "SUBSCRIPTION_PROFILE_COMPAT_DELETE_FAILED",
                },
            )
            return {
                "ok": False,
                "status": "failed",
                "stage": "delete_compatibility_client",
                "error_code": result.error_code or "SUBSCRIPTION_PROFILE_COMPAT_DELETE_FAILED",
                "error_message": result.message,
                "subscription_profile": disabled,
                "details": _strip_raw_payload(result.details),
            }
        deleted_compat_clients.append(
            {
                "client_id": client.client_id,
                "client_uuid": client.client_uuid,
                "email": client.email,
            }
        )
    if deleted_compat_clients:
        _sync_xray_inventory(requested_by)

    reconcile = reconcile_xray_subscription_profile_nodes(
        requested_by=requested_by,
        materialize=True,
        token_or_slug=token,
        cleanup_deleted_projections=False,
    )
    reconcile_cleanup = _merge_projection_cleanups(
        *(item.get("cleanup") for item in (reconcile.get("deleted") or []) if isinstance(item, dict))
    )
    cleanup = reconcile_cleanup
    account = disabled.get("account") if isinstance(disabled.get("account"), dict) else {}
    changed = (
        account_id is not None
        or bool(account.get("was_enabled"))
        or int(account.get("enabled_clients_count") or 0) > 0
        or bool(deleted_compat_clients)
        or int(cleanup.get("subjects_deleted") or 0) > 0
        or int(cleanup.get("server_overrides_deleted") or 0) > 0
        or int(cleanup.get("user_overrides_deleted") or 0) > 0
        or bool(reconcile.get("deleted"))
    )
    if not reconcile.get("ok"):
        if changed:
            write_operational_log(
                event_type="external_client.delete_failed",
                level="warning",
                subject_id=None,
                message="External client delete failed.",
                details={
                    "requested_by": safe_actor_identifier(requested_by),
                    "stage": "reconcile_subscription_profile_delete",
                    "error_code": reconcile.get("error_code") or "SUBSCRIPTION_PROFILE_RECONCILE_FAILED",
                    "subjects_deleted": int(cleanup.get("subjects_deleted") or 0),
                },
            )
        return {
            "ok": False,
            "status": "failed",
            "stage": "reconcile_subscription_profile_delete",
            "error_code": reconcile.get("error_code") or "SUBSCRIPTION_PROFILE_RECONCILE_FAILED",
            "error_message": reconcile.get("error_message") or "Subscription profile reconcile failed.",
            "cleanup": {key: value for key, value in cleanup.items() if key != "subject_ids"},
        }

    if account_id is None:
        account = disabled.get("account") if isinstance(disabled.get("account"), dict) else {}
        account_id = int(account.get("account_id") or 0) or None
    if account_id is None:
        return {"ok": False, "status": "failed", "stage": "resolve_account", "error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND", "error_message": "Subscription profile was not found."}
    try:
        with db_session() as connection:
            # The durable identity tables intentionally outlive subscription
            # rows, so account deletion must tombstone every credential scope
            # owned by this account before its tokens cascade away. Keep this
            # in the same transaction as the final account DELETE: a stale
            # delete target or projection-cleanup failure must not revoke a
            # still-existing account. Native removal and profile reconciliation
            # have already succeeded above while this function owns the Xray
            # writer reservation.
            account_tokens = [
                str(row["token"])
                for row in connection.execute(
                    "SELECT token FROM subscription_clients WHERE account_id=? ORDER BY client_id",
                    (int(account_id),),
                )
            ]
            from fwrouter_api.services.subscription_node_identity import (
                IdentityConflict, revoke_credentials,
            )
            try:
                for account_token in account_tokens:
                    scope = "subscription:" + account_token
                    credential_rows = connection.execute(
                        "SELECT identity_id, epoch, revoked FROM subscription_node_credentials WHERE scope=?",
                        (scope,),
                    ).fetchall()
                    for credential in credential_rows:
                        if bool(credential["revoked"]):
                            continue
                        revoke_credentials(
                            connection,
                            scope=scope,
                            identity_id=str(credential["identity_id"]),
                            expected_epoch=int(credential["epoch"]),
                        )
            except IdentityConflict as exc:
                raise _SubscriptionProfileCredentialRevocationFailed() from exc

            cleanup = _merge_projection_cleanups(
                cleanup,
                cleanup_xray_subscription_profile_projection(token, connection=connection),
            )
            cursor = connection.execute("DELETE FROM subscription_accounts WHERE account_id = ? AND slug = ?", (int(account_id), token))
            if cursor.rowcount != 1:
                raise _SubscriptionProfileDeleteAccountMismatch()
    except _SubscriptionProfileDeleteAccountMismatch:
        return {"ok": False, "status": "failed", "stage": "delete_account", "error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND", "error_message": "Subscription profile was not found."}
    except _SubscriptionProfileCredentialRevocationFailed:
        return {
            "ok": False,
            "status": "failed",
            "stage": "revoke_subscription_credentials",
            "error_code": "SUBSCRIPTION_CREDENTIAL_REVOCATION_FAILED",
            "error_message": "Subscription credentials could not be safely revoked.",
        }

    payload = {
        "ok": True,
        "status": "success",
        "stage": "completed" if changed else "noop",
        "subscription_ref": "sub-profile:" + hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "deleted_compatibility_clients_count": len(deleted_compat_clients),
        "reconcile": {"nodes_count": int(reconcile.get("nodes_count") or 0), "created_count": int(reconcile.get("created_count") or 0), "deleted_count": int(reconcile.get("deleted_count") or 0)},
        "cleanup": {key: value for key, value in cleanup.items() if key != "subject_ids"},
        "noop": not changed,
    }
    if changed:
        write_operational_log(
            event_type="external_client.deleted",
            level="info",
            subject_id=None,
            message="External client deleted.",
            details={
                "subscription_ref": "sub-profile:" + hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "requested_by": safe_actor_identifier(requested_by),
                "result": "success",
                "subjects_deleted": int(cleanup.get("subjects_deleted") or 0),
                "deleted_compatibility_clients": len(deleted_compat_clients),
            },
        )
    else:
        write_operational_log(
            event_type="external_client.delete_noop",
            level="info",
            subject_id=None,
            message="External client delete noop.",
            details={
                "subscription_ref": "sub-profile:" + hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "requested_by": safe_actor_identifier(requested_by),
                "result": "noop",
                "subjects_deleted": int(cleanup.get("subjects_deleted") or 0),
                "deleted_compatibility_clients": len(deleted_compat_clients),
            },
        )
    return {
        **payload,
    }


def submit_xray_subscription_profile_delete(
    token_or_slug: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    supplied_identity = str(token_or_slug or "").strip().lower()
    token = supplied_identity
    account_id: int | None = None
    if supplied_identity.startswith(XRAY_SUBSCRIPTION_ACCOUNT_DELETE_REF_PREFIX):
        raw_account_id = supplied_identity[len(XRAY_SUBSCRIPTION_ACCOUNT_DELETE_REF_PREFIX):]
        if not raw_account_id.isascii() or not raw_account_id.isdecimal() or int(raw_account_id) <= 0 or str(int(raw_account_id)) != raw_account_id:
            return {
                "ok": False,
                "status": "failed",
                "result": {
                    "error_code": "SUBSCRIPTION_PROFILE_DELETE_REF_INVALID",
                    "message": "Subscription profile delete reference is invalid.",
                },
            }
        with db_session() as connection:
            account = connection.execute(
                "SELECT account_id, slug FROM subscription_accounts WHERE account_id = ? LIMIT 1",
                (int(raw_account_id),),
            ).fetchone()
        if account is None:
            return {
                "ok": False,
                "status": "failed",
                "result": {
                    "error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND",
                    "message": "Subscription profile was not found.",
                },
            }
        token = str(account["slug"] or "").strip().lower()
        account_id = int(account["account_id"])
        if not token:
            return {
                "ok": False,
                "status": "failed",
                "result": {
                    "error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND",
                    "message": "Subscription profile was not found.",
                },
            }
    else:
        with db_session() as connection:
            account = connection.execute(
                "SELECT sa.account_id, sa.slug FROM subscription_accounts AS sa LEFT JOIN subscription_clients AS sc ON sc.account_id = sa.account_id WHERE lower(sa.slug) = ? OR lower(sc.token) = ? LIMIT 1",
                (token, token),
            ).fetchone()
        if account is None:
            return {"ok": False, "status": "failed", "result": {"error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND", "message": "Subscription profile was not found."}}
        token = str(account["slug"] or "").strip().lower()
        account_id = int(account["account_id"])
    if account_id is None:
        return {"ok": False, "status": "failed", "result": {"error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND", "message": "Subscription profile was not found."}}
    if not _subscription_delete_account_supported(account_id, token):
        return {"ok": False, "status": "failed", "result": {"error_code": "SUBSCRIPTION_PROFILE_CLIENT_SET_UNSUPPORTED", "message": "Subscription profile client set is not supported for deletion."}}
    manager = get_default_job_manager()
    lock_key = f"xray-subscription-profile-delete:{account_id}"
    try:
        job = manager.create(
            XRAY_SUBSCRIPTION_PROFILE_DELETE_JOB_TYPE,
            lock_key=lock_key,
            requested_by=requested_by,
            input_data={"token": token, "account_id": account_id, "requested_by": requested_by},
        )
    except JobLockConflictError as exc:
        return {
            "ok": True,
            "status": "accepted",
            "stage": "existing_job",
            "job": exc.active_job,
            "result": {
                "message": "External client delete job is already running.",
                "error_code": None,
                "details": {"lock_key": lock_key},
            },
        }
    job = manager.start_job_and_wait(job["job_id"], timeout_seconds=1) or job
    return {
        "ok": True,
        "status": "accepted",
        "stage": "queued" if str(job.get("status")) == "queued" else "running",
        "job": job,
        "result": {
            "message": "External client delete accepted.",
            "error_code": None,
            "details": {"lock_key": lock_key},
        },
    }


def run_xray_subscription_profile_delete_job(job: dict[str, Any]) -> dict[str, Any]:
    input_data = job.get("input") if isinstance(job.get("input"), dict) else {}
    try:
        account_id = int(input_data.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    if account_id <= 0:
        return {"job_status": "failed", "status": "failed", "error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND", "error_message": "Subscription profile was not found."}
    payload = delete_xray_subscription_profile(
        str(input_data.get("token") or ""),
        account_id=account_id,
        requested_by=str(input_data.get("requested_by") or job.get("requested_by") or "job"),
    )
    if not payload.get("ok"):
        return {
            "job_status": "failed",
            "status": "failed",
            "error_code": payload.get("error_code") or payload.get("result", {}).get("error_code") or "SUBSCRIPTION_PROFILE_DELETE_FAILED",
            "error_message": "External client delete failed.",
            "subscription_profile": {key: payload[key] for key in ("ok", "status", "stage", "error_code", "error_message", "cleanup") if key in payload},
        }
    return {
        "job_status": "success",
        "status": "success",
        "subscription_profile": payload,
    }


def export_xray_vpn_auto_subscription_text(
    *,
    base64_encode: bool = True,
    requested_by: str = "api",
) -> dict[str, Any]:
    servers = _vpn_auto_servers_for_xray_subscription()
    if not servers:
        return {
            "ok": False,
            "content": "",
            "uris": [],
            "nodes_count": 0,
            "error_code": "XRAY_VPN_AUTO_EMPTY",
            "error_message": "No supported vpn-auto servers are available for Xray subscription.",
        }

    existing_clients = {
        str(client.email or ""): client
        for client in _xray_adapter().list_clients()
        if str(client.email or "")
    }

    nodes: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for server in servers:
        server_id = str(server["server_id"])
        server_name = str(server["server_name"] or server_id)
        email = _vpn_auto_xray_client_email(server_id)

        client = existing_clients.get(email)
        if client is None:
            missing.append(
                {
                    "server_id": server_id,
                    "server_name": server_name,
                    "email": email,
                }
            )
            continue

        nodes.append(
            {
                "server_id": server_id,
                "server_name": server_name,
                "client": client,
                "uri": _full_xray_client_uri(client, display_name=server_name),
            }
        )

    materialize = {
        "ok": True,
        "status": "skipped",
        "reason": "subscription_read_only_export",
    }

    if not nodes and missing:
        return {
            "ok": False,
            "content": "",
            "uris": [],
            "nodes_count": 0,
            "missing_count": len(missing),
            "missing": missing,
            "error_code": "XRAY_VPN_AUTO_CLIENTS_NOT_MATERIALIZED",
            "error_message": "Xray vpn-auto subscription clients are not materialized.",
            "materialize": materialize,
        }

    raw_content = chr(10).join(node["uri"] for node in nodes) + chr(10)
    content = (
        base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
        if base64_encode
        else raw_content
    )

    return {
        "ok": True,
        "content": content,
        "uris": [node["uri"] for node in nodes],
        "base64": base64_encode,
        "nodes_count": len(nodes),
        "missing_count": len(missing),
        "missing": missing,
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "client_id": node["client"].client_id,
                "client_uuid": node["client"].client_uuid,
                "email": node["client"].email,
            }
            for node in nodes
        ],
        "materialize": materialize,
    }


def export_xray_subscription_text(
    client_id: str,
    *,
    base64_encode: bool = True,
) -> dict[str, Any]:
    aliases = _client_alias_map()

    target: XrayClient | None = None
    for client in _xray_adapter().list_clients():
        if client.client_id == client_id or client.client_uuid == client_id:
            target = client
            break

    if target is None:
        return {
            "ok": False,
            "content": "",
            "uris": [],
            "error_code": "XRAY_CLIENT_NOT_FOUND",
            "error_message": f"Xray client not found: {client_id}",
        }

    uri = _full_xray_client_uri(
        target,
        display_name=aliases.get(target.client_id) or aliases.get(target.client_uuid),
    )
    raw_content = uri + "\n"
    content = (
        base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
        if base64_encode
        else raw_content
    )

    return {
        "ok": True,
        "content": content,
        "uris": [uri],
        "base64": base64_encode,
        "nodes_count": 1,
        "client_id": target.client_id,
        "client_uuid": target.client_uuid,
    }


def export_xray_subscription(client_id: str) -> dict[str, Any]:
    result = _xray_adapter().export_vless_subscription(client_id)
    details = dict(result.details)
    public_endpoint = configured_xray_public_endpoint()
    subject = _xray_subject_for_client(client_id)
    effective_state = subject.get("effective_state") if isinstance(subject, dict) and isinstance(subject.get("effective_state"), dict) else {}
    scoped_runtime = effective_state.get("scoped_runtime") if isinstance(effective_state.get("scoped_runtime"), dict) else None
    return {
        "ok": result.ok,
        "client_id": client_id,
        "public_host": public_endpoint["host"],
        "public_port": public_endpoint["port"],
        "public_path": public_endpoint["path"],
        "transport": "ws",
        "security": "tls",
        "subscription_uri": details.get("subscription_uri"),
        "subject_id": subject.get("subject_id") if isinstance(subject, dict) else None,
        "server_binding": {
            "selected_server_id": effective_state.get("selected_server_id"),
            "selected_server_source": effective_state.get("selected_server_source"),
            "effective_mode": effective_state.get("effective_mode"),
            "dataplane_path": effective_state.get("dataplane_path"),
            "scoped_runtime": scoped_runtime,
            "binding_saved": bool(effective_state.get("selected_server_id")),
            "binding_applied": bool(scoped_runtime and scoped_runtime.get("status") == "applied"),
            "binding_verified": bool(scoped_runtime and scoped_runtime.get("status") == "applied"),
        },
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": details,
        },
    }
