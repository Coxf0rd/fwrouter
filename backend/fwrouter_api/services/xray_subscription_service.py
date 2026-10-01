from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fwrouter_api.services.xray_subscription import configured_xray_public_endpoint
from fwrouter_api.adapters.xray import XRAY_PUBLIC_PATH, XRAY_PUBLIC_PORT, XrayClient
from fwrouter_api.adapters.xray_common import xray_writer_guarded
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
from fwrouter_api.services.subjects import get_subject
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
        current = _xray_subject_for_client(client_uuid)
        if isinstance(current, dict):
            subject_id = str(current.get("subject_id") or f"xray:{client_uuid}")
            subject = get_subject(subject_id) or current
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
    from fwrouter_api.services.subscription_pipeline import validate_mihomo_candidate_config as validate_native_mihomo

    native: dict[str, Any] = {"xray": dict(xray_result.details)}
    staged_mihomo: dict[str, str] = {}
    for name, handoffs in (("transition", transition_assignments), ("final", assignments)):
        candidate_path = stage_dir / f"mihomo-{name}.candidate.yaml"
        written = write_mihomo_candidate_config(
            candidate_path=candidate_path,
            xray_handoff_assignments=handoffs,
            include_internal_config=True,
        )
        candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        candidate_config = written.get("_candidate_config") if isinstance(written.get("_candidate_config"), dict) else {}
        local_validation = validate_local_mihomo(candidate_path=candidate_path, candidate_config=candidate_config)
        native_validation = validate_native_mihomo(str(candidate_path))
        validation = {
            "ok": bool(local_validation.get("ok")) and bool(native_validation.get("ok")),
            "local": _strip_raw_payload(local_validation),
            "native": _strip_raw_payload(native_validation),
        }
        if not validation["ok"]:
            return {
                "ok": False,
                "stage": f"mihomo_{name}_candidate",
                "error_code": "MIHOMO_CANDIDATE_INVALID",
                "validation": _strip_raw_payload(validation),
            }
        if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != candidate_hash:
            return {"ok": False, "stage": f"mihomo_{name}_candidate", "error_code": "MIHOMO_STAGE_CANDIDATE_CHANGED_DURING_VALIDATION"}
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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _generation_source_fingerprint() -> str:
    with db_session() as connection:
        sources = [
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
            [dict(row) for row in connection.execute("SELECT desired_mode, selective_default, server_mode, active_auto_server_id FROM routing_global_state ORDER BY id").fetchall()],
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
) -> Path:
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.artifacts import atomic_write_text

    checkpoint_path = _xray_generation_checkpoint_path(adapter)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.chmod(0o700)
    if checkpoint_path.exists():
        raise RuntimeError("An unresolved Xray generation checkpoint already exists.")
    xray_path = Path(adapter.config_path)
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    binding_path = _xray_bindings_path()
    with db_session() as connection:
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
            "mihomo_final_path": (staged_generation.get("mihomo_candidates") or {}).get("final"),
            "mihomo_final_sha256": ((staged_generation.get("native_validation") or {}).get("final") or {}).get("candidate_sha256"),
        },
    }
    atomic_write_text(checkpoint_path, json.dumps(data, sort_keys=True))
    checkpoint_path.chmod(0o600)
    _fsync_directory(checkpoint_path.parent)
    return checkpoint_path


def _update_xray_generation_checkpoint(checkpoint_path: Path, *, phase: str) -> None:
    from fwrouter_api.services.artifacts import atomic_write_text

    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    data["phase"] = phase
    atomic_write_text(checkpoint_path, json.dumps(data, sort_keys=True))
    checkpoint_path.chmod(0o600)
    _fsync_directory(checkpoint_path.parent)


def _capture_generation_auto_selection() -> dict[str, Any]:
    with db_session() as connection:
        routing = connection.execute(
            "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, active_auto_server_id, updated_at FROM routing_global_state WHERE id = 1"
        ).fetchone()
        provenance = connection.execute(
            "SELECT value_json, updated_at FROM settings WHERE key = 'routing.auto_selection_provenance'"
        ).fetchone()
    return {
        "routing": dict(routing) if routing else None,
        "provenance": dict(provenance) if provenance else None,
    }


def _restore_generation_auto_selection(connection: Any, *, before: dict[str, Any], after: dict[str, Any]) -> bool:
    routing = connection.execute(
        "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, active_auto_server_id, updated_at FROM routing_global_state WHERE id = 1"
    ).fetchone()
    provenance = connection.execute(
        "SELECT value_json, updated_at FROM settings WHERE key = 'routing.auto_selection_provenance'"
    ).fetchone()
    current = {
        "routing": dict(routing) if routing else None,
        "provenance": dict(provenance) if provenance else None,
    }
    if current != after:
        return False
    previous_routing = before.get("routing")
    if previous_routing:
        connection.execute(
            "UPDATE routing_global_state SET active_auto_server_id = ?, updated_at = ? WHERE id = 1",
            (previous_routing.get("active_auto_server_id"), previous_routing.get("updated_at")),
        )
    previous_provenance = before.get("provenance")
    if previous_provenance:
        connection.execute(
            "UPDATE settings SET value_json = ?, updated_at = ? WHERE key = 'routing.auto_selection_provenance'",
            (previous_provenance.get("value_json"), previous_provenance.get("updated_at")),
        )
    else:
        connection.execute("DELETE FROM settings WHERE key = 'routing.auto_selection_provenance'")
    return True


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


def _mark_generation_selection_verification_required(checkpoint_path: Path) -> None:
    from fwrouter_api.services.artifacts import atomic_write_text
    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    data["selection_verification_required"] = True
    atomic_write_text(checkpoint_path, json.dumps(data, sort_keys=True))
    checkpoint_path.chmod(0o600)
    _fsync_directory(checkpoint_path.parent)


def _record_xray_generation_derived_rows(checkpoint_path: Path, *, phase: str) -> None:
    from fwrouter_api.services.artifacts import atomic_write_text

    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    data["auto_selection_after"] = _capture_generation_auto_selection()
    data["derived_rows_after"] = _capture_generation_derived_rows(
        managed_email_prefixes=list(data.get("managed_email_prefixes") or []),
        expected_client_identities=list(data.get("expected_client_identities") or []),
    )
    data["derived_source_fingerprint"] = _generation_source_fingerprint()
    data["phase"] = phase
    atomic_write_text(checkpoint_path, json.dumps(data, sort_keys=True))
    checkpoint_path.chmod(0o600)
    _fsync_directory(checkpoint_path.parent)


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
    atomic_write_text(checkpoint_path, json.dumps(data, sort_keys=True))
    checkpoint_path.chmod(0o600)
    _fsync_directory(checkpoint_path.parent)


def _restore_xray_generation_checkpoint(adapter: Any, checkpoint_path: Path) -> dict[str, Any]:
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.artifacts import atomic_write_text
    from fwrouter_api.services.mihomo_runtime import restart_mihomo_container

    data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
    for key in ("xray_config", "mihomo_config", "xray_bindings"):
        artifact = artifacts.get(key) if isinstance(artifacts.get(key), dict) else {}
        text_value = artifact.get("text")
        target = Path(artifact.get("path") or "")
        if not isinstance(text_value, str):
            target.unlink(missing_ok=True)
            continue
        raw = base64.b64decode(text_value)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, raw.decode("utf-8"))
    xray_restore = adapter.reload()
    mihomo_restore = restart_mihomo_container(action="force_recreate")
    snapshots = data.get("subscription_snapshots") if isinstance(data.get("subscription_snapshots"), dict) else {}
    snapshots_after = data.get("subscription_snapshots_after")
    derived_rows_after = data.get("derived_rows_after")
    derived_rows_before = data.get("derived_rows_before") if isinstance(data.get("derived_rows_before"), dict) else {}
    derived_restore_ok = True
    current_fingerprint = _generation_source_fingerprint() if isinstance(derived_rows_after, dict) else None
    if derived_rows_after is None and current_fingerprint is None:
        current_fingerprint = _generation_source_fingerprint()
    if isinstance(derived_rows_after, dict) and current_fingerprint != str(data.get("derived_source_fingerprint") or ""):
        derived_restore_ok = False
    if derived_rows_after is None and current_fingerprint != str(data.get("source_fingerprint") or ""):
        derived_restore_ok = False
    current_snapshots: dict[str, dict[str, Any] | None] = {}
    with db_session() as connection:
        for token in snapshots:
            row = connection.execute(
                "SELECT token, nodes_json, runtime_verified_at, updated_at FROM subscription_profile_snapshots WHERE token = ?",
                (token,),
            ).fetchone()
            current_snapshots[token] = dict(row) if row else None
    expected_snapshots = snapshots_after if isinstance(snapshots_after, dict) else snapshots
    if current_snapshots != expected_snapshots:
        derived_restore_ok = False
    with db_session() as connection:
        if derived_restore_ok and isinstance(derived_rows_after, dict):
            derived_restore_ok = _restore_scoped_generation_rows(
                    connection,
                    before=derived_rows_before,
                    after=derived_rows_after,
                )
        if derived_restore_ok and isinstance(data.get("auto_selection_after"), dict):
            derived_restore_ok = _restore_generation_auto_selection(
                connection,
                before=data.get("auto_selection_before") or {},
                after=data.get("auto_selection_after") or {},
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
    selection_readback = (
        _verify_generation_selection_readback(data.get("auto_selection_before"))
        if derived_restore_ok and data.get("selection_verification_required") and isinstance(data.get("auto_selection_before"), dict)
        else {"ok": derived_restore_ok}
    )
    derived_restore_ok = bool(derived_restore_ok and selection_readback.get("ok"))
    ok = bool(xray_restore.ok) and bool(mihomo_restore.get("ok")) and derived_restore_ok
    if ok:
        checkpoint_path.unlink(missing_ok=True)
        _fsync_directory(checkpoint_path.parent)
    else:
        _update_xray_generation_checkpoint(checkpoint_path, phase="restore_failed")
    return {"ok": ok, "recovered": "restored_last_good", "xray_reload": xray_restore.ok, "mihomo_restart": bool(mihomo_restore.get("ok")), "derived_rows_restored": derived_restore_ok, "selection_readback": selection_readback}


@xray_writer_guarded
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


@xray_writer_guarded
def reconcile_xray_subscription_profile_nodes(
    *,
    requested_by: str = "api",
    materialize: bool = True,
    token_or_slug: str | None = None,
    promote_public_profile: bool = True,
    cleanup_deleted_projections: bool = True,
    preserve_existing_overrides: bool = False,
    include_vpn_auto: bool = False,
    verification_callback: Any = None,
) -> dict[str, Any]:
    adapter = _xray_adapter()
    checkpoint_path = _xray_generation_checkpoint_path(adapter)
    if checkpoint_path.exists():
        recovered = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
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

    source_fingerprint = _generation_source_fingerprint()
    subscription_nodes = list_desired_subscription_xray_clients(token_or_slug)
    desired_nodes = list(subscription_nodes)
    affected_profile_tokens = list_subscription_profile_tokens(token_or_slug)
    existing_clients = list(adapter.list_clients())
    if include_vpn_auto and token_or_slug is None:
        existing_auto = {
            str(client.email or "").lower(): client
            for client in existing_clients
            if str(client.email or "").lower().startswith("vpn-auto-")
        }
        for server in _vpn_auto_servers_for_xray_subscription():
            server_id = str(server["server_id"])
            email = _vpn_auto_xray_client_email(server_id)
            client = existing_auto.get(email.lower())
            client_uuid = str(client.client_uuid or client.client_id) if client else str(uuid4())
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
        _update_xray_generation_checkpoint(checkpoint_path, phase="transition_applied")
        applied_xray = adapter.apply_staged_subscription_generation(
            staged_generation["xray_candidate_path"],
            expected_sha256=staged_generation["xray_candidate_sha256"],
        )
        if not applied_xray.ok:
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {"ok": False, "status": "failed", "stage": "xray_generation_apply", "error_code": applied_xray.error_code or "XRAY_GENERATION_APPLY_FAILED", "details": _strip_raw_payload(applied_xray.details)}
        _update_xray_generation_checkpoint(checkpoint_path, phase="xray_applied")
        final_path = staged_generation["mihomo_candidates"]["final"]
        applied_final_mihomo = _apply_staged_mihomo_candidate(
            final_path,
            staged_generation["native_validation"]["final"]["candidate_sha256"],
        )
        if not applied_final_mihomo.get("ok"):
            restored = _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {"ok": False, "status": "failed", "stage": "mihomo_final_apply", "error_code": "XRAY_GENERATION_FINAL_APPLY_FAILED", "details": _strip_raw_payload(applied_final_mihomo)}
        _update_xray_generation_checkpoint(checkpoint_path, phase="runtime_applied")
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
            _restore_xray_generation_checkpoint(adapter, checkpoint_path)
        return {**binding_result, "status": "failed"}
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
                _restore_xray_generation_checkpoint(adapter, checkpoint_path)
            return {
                "ok": False,
                "status": "failed",
                "stage": "materialize",
                "error_code": "XRAY_SUB_PROFILE_MATERIALIZE_FAILED",
                "error_message": "Failed to materialize Xray subscription profile bindings.",
                "materialize": materialize_result,
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

    promoted_profile = (
        promote_runtime_verified_subscription_nodes(
            subscription_nodes,
            profile_tokens=affected_profile_tokens,
        )
        if materialize and promote_public_profile
        else {"profiles_count": 0, "nodes_count": 0}
    )
    if staged_generation is not None and checkpoint_path.exists():
        _record_xray_generation_snapshot_postimage(checkpoint_path)

    generation_apply = None
    if staged_generation is not None:
        generation_apply = {
            "transition_mihomo": _strip_raw_payload(applied_transition),
            "xray": _strip_raw_payload(applied_xray.details),
            "final_mihomo": _strip_raw_payload(applied_final_mihomo),
            "public_snapshots_changed": _xray_generation_snapshots_changed(checkpoint_path),
        }
    if staged_generation is not None and checkpoint_path.exists():
        checkpoint_path.unlink(missing_ok=True)
        _fsync_directory(checkpoint_path.parent)

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
        "generation_apply": generation_apply,
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
