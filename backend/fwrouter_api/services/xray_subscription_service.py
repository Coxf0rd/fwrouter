from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fwrouter_api.services.xray_subscription import configured_xray_public_endpoint
from fwrouter_api.adapters.xray import XRAY_PUBLIC_PATH, XRAY_PUBLIC_PORT, XrayClient
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
    _set_local_alias,
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


XRAY_SUBSCRIPTION_PROFILE_DELETE_JOB_TYPE = "xray_subscription_profile_delete"
XRAY_SUBSCRIPTION_ACCOUNT_DELETE_REF_PREFIX = "subscription-account:"


class _SubscriptionProfileDeleteAccountMismatch(RuntimeError):
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
                                         legacy_native_clients: list[Any] | None = None) -> None:
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
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
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
            connection.execute(
                "UPDATE servers SET raw_json=? WHERE server_id=? AND inventory_state='active'",
                (json.dumps(target_raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")), target_id),
            )
            changed += int(prior is None)
        revision = current_revision
        if changed:
            revision = advance_selection_revision(connection, expected_revision=current_revision)
            if revision is None:
                raise IdentityConflict("IDENTITY_MIGRATION_SELECTION_STALE")
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
            f"""
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
    bindings = [item for item in collect_xray_runtime_bindings() if not scoped_email(item)]
    modes = [item for item in collect_xray_client_mode_directives() if not scoped_email(item)]
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


def _stage_profile_native_candidates(
    *,
    adapter: Any,
    desired_clients: list[dict[str, Any]],
    managed_email_prefixes: list[str],
    bindings: list[dict[str, Any]],
    client_modes: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Native-validate prospective Xray plus transition/final Mihomo configs."""
    config_path = getattr(adapter, "config_path", None)
    if config_path is None or not callable(getattr(adapter, "stage_subscription_generation", None)):
        return {"ok": False, "stage": "stage_api", "error_code": "XRAY_GENERATION_STAGE_UNAVAILABLE"}
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
            fingerprint = current_mihomo_input_fingerprint().get("hash")
            if not isinstance(fingerprint, str) or len(fingerprint) != 64:
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
        local_validation = validate_local_mihomo(candidate_path=candidate_path, candidate_config=candidate_config)
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
        if write_reused and candidate_input_context() != transition_context:
            return {"ok": False, "stage": "mihomo_final_candidate", "error_code": "MIHOMO_STAGE_INPUTS_CHANGED_AFTER_VALIDATION"}
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
    }


def _apply_staged_mihomo_candidate(candidate_path: str, expected_sha256: str) -> dict[str, Any]:
    source = Path(candidate_path)
    if not source.exists() or hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
        return {"ok": False, "stage": "candidate_integrity", "error_code": "MIHOMO_STAGE_CANDIDATE_CHANGED_AFTER_VALIDATION"}
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.artifacts import atomic_copy_file
    from fwrouter_api.services.mihomo_runtime import restart_mihomo_container

    active_path = Path(mihomo_config._resolved_base_config_path())
    if active_path.exists() and hashlib.sha256(active_path.read_bytes()).hexdigest() == expected_sha256:
        return {"ok": True, "stage": "unchanged", "container": {"ok": True, "action": "none", "reason": "config_unchanged"}}
    active_candidate = Path(mihomo_config._resolved_candidate_config_path())
    atomic_copy_file(source, active_candidate)
    if hashlib.sha256(active_candidate.read_bytes()).hexdigest() != expected_sha256:
        return {"ok": False, "stage": "candidate_integrity", "error_code": "MIHOMO_STAGE_CANDIDATE_COPY_MISMATCH"}
    promoted = mihomo_config.promote_mihomo_candidate_config()
    if not promoted.get("ok"):
        return {"ok": False, "stage": "promote", "promoted": _strip_raw_payload(promoted)}
    restarted = restart_mihomo_container(action="force_recreate")
    return {
        "ok": bool(restarted.get("ok")),
        "stage": "applied" if restarted.get("ok") else "restart",
        "promoted": _strip_raw_payload(promoted),
        "container": _strip_raw_payload(restarted),
    }


def _xray_generation_checkpoint_path(adapter: Any) -> Path:
    return Path(adapter.config_path).parent / ".generation" / "generation-checkpoint.json"


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


def _generation_source_fingerprint() -> str:
    with db_session() as connection:
        sources = [
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
            [dict(row) for row in connection.execute("SELECT subject_id, selected_server_id, selected_until FROM subject_server_overrides ORDER BY subject_id").fetchall()],
            [dict(row) for row in connection.execute("SELECT subject_id, override_mode, override_until FROM subject_user_overrides ORDER BY subject_id").fetchall()],
            [dict(row) for row in connection.execute("SELECT desired_mode, selective_default, server_mode FROM routing_global_state ORDER BY id").fetchall()],
            [dict(row) for row in connection.execute("SELECT key, value_json FROM settings WHERE key='vpn_auto_exclusive_source_ref'").fetchall()],
            [dict(row) for row in connection.execute("SELECT source_ref, enabled, logical_server_id, provider_id, resource_id, protocol, binding_revision FROM provider_bindings ORDER BY source_ref").fetchall()],
            [dict(row) for row in connection.execute("SELECT source_ref, provider_member_id, location_id, protocol, provider_status, auto_enabled, priority, advertised FROM provider_members ORDER BY source_ref, provider_member_id, location_id, protocol").fetchall()],
        ]
    return hashlib.sha256(json.dumps(sources, sort_keys=True, default=str).encode()).hexdigest()


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
) -> Path:
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    checkpoint_path = _xray_generation_checkpoint_path(adapter)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.chmod(0o700)
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
        "auto_selection_before": _capture_generation_auto_selection(),
        "auto_selection_after": None,
        "managed_email_prefixes": managed_email_prefixes,
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


def _capture_generation_auto_selection() -> dict[str, Any]:
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_fence
    with db_session() as connection:
        routing = connection.execute(
            "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, active_auto_server_id, updated_at FROM routing_global_state WHERE id = 1"
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
        mihomo_incarnation = get_mihomo_runtime_incarnation()
        source_fingerprint = _generation_source_fingerprint()
        projection_fingerprint = _current_projection_fingerprint(projection)
        selection = _capture_generation_auto_selection()
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
                if not _verify_generation_selection_readback(selection).get("ok"):
                    return False
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
        selection_readback = _verify_generation_selection_readback(selection)
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


def _record_xray_generation_derived_rows(checkpoint_path: Path, *, phase: str) -> None:
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    data["auto_selection_after"] = _capture_generation_auto_selection()
    data["derived_rows_after"] = _capture_generation_derived_rows(
        managed_email_prefixes=list(data.get("managed_email_prefixes") or []),
        expected_client_identities=list(data.get("expected_client_identities") or []),
    )
    data["derived_source_fingerprint"] = _generation_source_fingerprint()
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
    from fwrouter_api.services.artifacts import atomic_write_text

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


def _publish_mihomo_udp_receipt_with_owner(
    *, expected_mihomo_runtime: str, expected_mihomo_sha: str,
    reconcile_reason: str, operation_id: str, owner_matches: Any,
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
        fingerprint = current_mihomo_input_fingerprint()
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
                current_fingerprint = current_mihomo_input_fingerprint()
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
                )
            except (OSError, ValueError, TypeError):
                return False

        return _publish_mihomo_udp_receipt_with_owner(
            expected_mihomo_runtime=expected_mihomo_runtime,
            expected_mihomo_sha=expected_mihomo_sha,
            reconcile_reason="xray_staged_generation",
            operation_id=generation_id,
            owner_matches=owned,
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
                current_fingerprint = current_mihomo_input_fingerprint()
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
        from fwrouter_api.services import mihomo_config
        from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

        def publication_context_matches() -> bool:
            try:
                with db_session() as connection:
                    fence = read_selection_fence(connection)
                return bool(
                    expected_source
                    and _generation_source_fingerprint() == expected_source
                    and type(expected_revision) is int
                    and fence.get("revision") == expected_revision
                    and (verification.get("selection_revision") in (None, expected_revision))
                    and expected_mihomo_runtime
                    and get_mihomo_runtime_incarnation() == expected_mihomo_runtime
                    and expected_mihomo_sha
                    and hashlib.sha256(Path(mihomo_config._resolved_base_config_path()).read_bytes()).hexdigest()
                    == expected_mihomo_sha
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
        return {"ok": False, "status": "pending", "stage": "xray_runtime_readback",
                "error_code": "XRAY_GENERATION_RUNTIME_READBACK_FAILED", "last_good_retained": True}
    if type(expected_revision) is not int or not _publish_xray_generation_mihomo_udp_receipt(
        checkpoint_path=checkpoint_path,
        verification=verification,
        expected_source=expected_source,
        expected_revision=expected_revision,
        expected_mihomo_runtime=expected_mihomo_runtime,
        expected_mihomo_sha=expected_mihomo_sha,
    ):
        return {"ok": False, "status": "pending", "stage": "mihomo_udp_policy_receipt",
                "error_code": "MIHOMO_UDP_POLICY_RECEIPT_UNCONFIRMED", "last_good_retained": True}
    promoted_profile = (
        promote_runtime_verified_subscription_nodes(
            pending["subscription_nodes"],
            profile_tokens=pending["affected_profile_tokens"],
        )
        if pending["materialize"] and pending["promote_public_profile"]
        else {"profiles_count": 0, "nodes_count": 0}
    )
    if checkpoint_path.exists():
        _record_xray_generation_snapshot_postimage(checkpoint_path)
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
    if checkpoint_path.exists():
        checkpoint_path.unlink(missing_ok=True)
        _fsync_directory(checkpoint_path.parent)
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
            "xray_reload": xray_restore.ok,
            "mihomo_restart": bool(mihomo_restore.get("ok")), "derived_rows_restored": derived_restore_ok,
            "selection_readback": selection_readback, "xray_runtime_readback": xray_runtime_readback,
            "mihomo_runtime_readback": mihomo_runtime_readback,
            "selection_restore_revision": owned_selection_revision}


def reconcile_xray_vpn_auto_subscription(
    *,
    requested_by: str = "api",
    verification_callback: Any = None,
) -> dict[str, Any]:
    result = reconcile_xray_subscription_profile_nodes(
        requested_by=requested_by,
        include_vpn_auto=True,
        verification_callback=verification_callback,
    )
    return {**result, "profile_reconcile": result}


def reconcile_xray_subscription_profile_nodes(
    *, requested_by: str = "api", materialize: bool = True,
    token_or_slug: str | None = None, promote_public_profile: bool = True,
    cleanup_deleted_projections: bool = True, preserve_existing_overrides: bool = False,
    include_vpn_auto: bool = False, verification_callback: Any = None,
    _supersede_checkpoint_sha256: str | None = None,
    _migration_plan_sha256: str | None = None,
) -> dict[str, Any]:
    """Run staged generation as guarded apply, unguarded verification, guarded publish."""
    with xray_writer_guard(timeout_seconds=30.0):
        from fwrouter_api.services.artifacts import atomic_write_text
        from fwrouter_api.services import mihomo_config
        result = _reconcile_xray_subscription_profile_nodes_guarded(
            requested_by=requested_by, materialize=materialize,
            token_or_slug=token_or_slug, promote_public_profile=promote_public_profile,
            cleanup_deleted_projections=cleanup_deleted_projections,
            preserve_existing_overrides=preserve_existing_overrides,
            include_vpn_auto=include_vpn_auto,
            verification_callback=verification_callback,
            _supersede_checkpoint_sha256=_supersede_checkpoint_sha256,
            _migration_plan_sha256=_migration_plan_sha256,
            _defer_selection_verification=callable(verification_callback),
        )
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
    _defer_selection_verification: bool = False,
    _supersede_checkpoint_sha256: str | None = None,
    _migration_plan_sha256: str | None = None,
) -> dict[str, Any]:
    adapter = _xray_adapter()
    checkpoint_path = _xray_generation_checkpoint_path(adapter)
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
        checkpoint_digest = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
        with db_session() as connection:
            migration_tied_to_checkpoint = connection.execute(
                "SELECT 1 FROM subscription_node_identity_transitions "
                "WHERE checkpoint_sha256=? LIMIT 1", (checkpoint_digest,),
            ).fetchone()
        if migration_tied_to_checkpoint:
            return {"ok": False, "status": "pending", "stage": "generation_supersession_required",
                    "error_code": "XRAY_GENERATION_SUPERSESSION_REQUIRED",
                    "last_good_retained": True}
        try:
            checkpoint_phase = str(json.loads(checkpoint_path.read_text(encoding="utf-8")).get("phase") or "")
        except Exception:
            checkpoint_phase = "invalid"
        if checkpoint_phase in {"restore_failed", "restore_started"}:
            recovered = _recover_checkpoint_from_current_projection(adapter, checkpoint_path)
        else:
            recovered = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            if not recovered.get("ok") and recovered.get("recovered") == "stale_checkpoint_declined":
                recovered = _recover_checkpoint_from_current_projection(adapter, checkpoint_path)
        if not recovered.get("ok"):
            return {
                "ok": False,
                "status": "pending",
                "stage": "generation_recovery",
                "error_code": "XRAY_GENERATION_RECOVERY_FAILED",
                "details": recovered,
            }

    blocked = _xray_managed_runtime_blocked("xray_subscription_profile_reconcile")
    if blocked is not None:
        return {
            **blocked,
            "ok": True,
            "status": "skipped",
            "reason": "managed_runtime_required",
            "nodes_count": 0,
        }

    module = _module_state("xray") or {}
    if str(module.get("desired_state") or "") != "enabled":
        return {
            "ok": True,
            "status": "skipped",
            "reason": "xray_module_disabled",
            "nodes_count": 0,
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
    _prepare_durable_profile_credentials(
        include_vpn_auto=include_vpn_auto and token_or_slug is None,
        legacy_native_clients=existing_clients,
    )
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
            return {"ok": False, "status": "failed", "stage": "generation_supersession_admission",
                    "error_code": code}
    source_fingerprint = _generation_source_fingerprint()
    subscription_nodes = list_desired_subscription_xray_clients(token_or_slug)
    desired_nodes = list(subscription_nodes)
    affected_profile_tokens = list_subscription_profile_tokens(token_or_slug)
    if include_vpn_auto and token_or_slug is None:
        from fwrouter_api.services.subscription_node_identity import credentials_for_scope, IdentityConflict
        with db_session() as connection:
            retained_auto = credentials_for_scope(connection, "vpn-auto")
        for server in _vpn_auto_servers_for_xray_subscription():
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
    staged_generation: dict[str, Any] | None = None
    if callable(getattr(adapter, "stage_subscription_generation", None)):
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
        )
        if not staged_generation.get("ok"):
            return {
                "ok": False,
                "status": "failed",
                "stage": staged_generation.get("stage") or "native_candidate_validation",
                "error_code": staged_generation.get("error_code") or "XRAY_GENERATION_CANDIDATE_INVALID",
                "details": _strip_raw_payload(staged_generation),
            }
        if _generation_source_fingerprint() != source_fingerprint:
            return {
                "ok": False,
                "status": "failed",
                "stage": "source_intent_changed",
                "error_code": "XRAY_GENERATION_SOURCE_CHANGED_DURING_STAGE",
            }
        if supersession is not None and not _checkpoint_supersession_state_unchanged(
            adapter, checkpoint_path, supersession["baseline"],
        ):
            return {"ok": False, "status": "failed", "stage": "generation_supersession_revalidation",
                    "error_code": "XRAY_SUPERSESSION_CONTEXT_STALE", "last_good_retained": True}
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
            )
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "stage": "checkpoint_prepare",
                "error_code": "XRAY_GENERATION_CHECKPOINT_FAILED",
                "error_message": str(exc),
            }
        transition = staged_generation["mihomo_candidates"]["transition"]
        applied_transition = _apply_staged_mihomo_candidate(
            transition,
            staged_generation["native_validation"]["transition"]["candidate_sha256"],
        )
        if not applied_transition.get("ok"):
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {"ok": False, "status": "failed", "stage": "mihomo_transition_apply", "error_code": "XRAY_GENERATION_TRANSITION_APPLY_FAILED", "details": _strip_raw_payload(applied_transition)}
        _update_xray_generation_checkpoint(
            checkpoint_path, phase="transition_applied",
            selection_revision=((applied_transition.get("container") or {}).get("selection_revision")),
        )
        applied_xray = adapter.apply_staged_subscription_generation(
            staged_generation["xray_candidate_path"],
            expected_sha256=staged_generation["xray_candidate_sha256"],
        )
        if not applied_xray.ok:
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {"ok": False, "status": "failed", "stage": "xray_generation_apply", "error_code": applied_xray.error_code or "XRAY_GENERATION_APPLY_FAILED", "details": _strip_raw_payload(applied_xray.details)}
        _update_xray_generation_checkpoint(
            checkpoint_path, phase="xray_applied",
            xray_runtime_incarnation=adapter.get_runtime_incarnation(),
            xray_mounted_config_sha256=adapter.get_runtime_config_sha256(),
        )
        final_path = staged_generation["mihomo_candidates"]["final"]
        applied_final_mihomo = _apply_staged_mihomo_candidate(
            final_path,
            staged_generation["native_validation"]["final"]["candidate_sha256"],
        )
        if not applied_final_mihomo.get("ok"):
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {"ok": False, "status": "failed", "stage": "mihomo_final_apply", "error_code": "XRAY_GENERATION_FINAL_APPLY_FAILED", "details": _strip_raw_payload(applied_final_mihomo), "generation_recovery": restored}
        _update_xray_generation_checkpoint(
            checkpoint_path, phase="runtime_applied",
            selection_revision=((applied_final_mihomo.get("container") or {}).get("selection_revision")),
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
            from fwrouter_api.services.artifacts import atomic_write_text
            checkpoint_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint_data["selection_operation_id"] = checkpoint_data.get("generation_id")
            _atomic_write_private_checkpoint(checkpoint_path, json.dumps(checkpoint_data, sort_keys=True))
            return {"_pending_generation": {
                "verification_callback": verification_callback,
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
            cleanup = _merge_projection_cleanups(
                cleanup,
                cleanup_xray_subscription_profile_projection(token, connection=connection),
            )
            cursor = connection.execute("DELETE FROM subscription_accounts WHERE account_id = ? AND slug = ?", (int(account_id), token))
            if cursor.rowcount != 1:
                raise _SubscriptionProfileDeleteAccountMismatch()
    except _SubscriptionProfileDeleteAccountMismatch:
        return {"ok": False, "status": "failed", "stage": "delete_account", "error_code": "SUBSCRIPTION_PROFILE_NOT_FOUND", "error_message": "Subscription profile was not found."}

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
