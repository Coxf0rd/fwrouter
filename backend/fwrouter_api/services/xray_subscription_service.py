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
from fwrouter_api.adapters.xray_common import xray_writer_guard, xray_writer_guarded
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
) -> Path:
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.artifacts import atomic_write_text
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    checkpoint_path = _xray_generation_checkpoint_path(adapter)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.parent.chmod(0o700)
    if checkpoint_path.exists():
        raise RuntimeError("An unresolved Xray generation checkpoint already exists.")
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
            "mihomo_final_path": (staged_generation.get("mihomo_candidates") or {}).get("final"),
            "mihomo_final_sha256": ((staged_generation.get("native_validation") or {}).get("final") or {}).get("candidate_sha256"),
        },
    }
    atomic_write_text(checkpoint_path, json.dumps(data, sort_keys=True))
    checkpoint_path.chmod(0o600)
    _fsync_directory(checkpoint_path.parent)
    return checkpoint_path


def _update_xray_generation_checkpoint(
    checkpoint_path: Path, *, phase: str, selection_revision: int | None = None,
    xray_runtime_incarnation: str | None = None, xray_mounted_config_sha256: str | None = None,
) -> None:
    from fwrouter_api.services.artifacts import atomic_write_text

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
    atomic_write_text(checkpoint_path, json.dumps(data, sort_keys=True))
    checkpoint_path.chmod(0o600)
    _fsync_directory(checkpoint_path.parent)


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
    expected_bindings = _bindings_for_state(bindings)
    expected_modes = [{key: item.get(key) for key in mode_keys} for item in modes]
    expected_handoffs = get_xray_handoff_listeners(bindings)
    parity = bool(
        state.get("bindings_version") == 1
        and normalized(state.get("bindings"), binding_keys) == normalized(expected_bindings, binding_keys)
        and normalized(state.get("client_modes"), mode_keys) == normalized(expected_modes, mode_keys)
        and state.get("handoff_listeners") == expected_handoffs
        and state.get("bindings_count") == len(bindings)
        and state.get("applied_count") == len(bindings)
        and all(item.get("status") == "applied" for item in state.get("bindings", []) if isinstance(item, dict))
        and state.get("client_modes_count") == len(modes)
        and all(item.get("status") == "applied" for item in state.get("client_modes", []) if isinstance(item, dict))
    )
    return parity, hashlib.sha256(raw).hexdigest()


def _verify_current_public_projection(snapshot: dict[str, Any]) -> bool:
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
                if bound_server_id != public_server_id:
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
                or not _verify_current_public_projection(projection)):
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
        if resume_owned_restart:
            after_incarnation = before_incarnation
        else:
            save_attempt("reload_started")
            atomic_write_text(config_path, candidate_text)
            reload_result = adapter.reload()
            if not reload_result.ok:
                return {"ok": False, "recovered": "current_projection_unproven", "reason": "xray_reload_failed"}
            after_incarnation = adapter.get_runtime_incarnation()
        if (adapter.get_runtime_config_sha256() != candidate_sha
                or (not resume_owned_restart and after_incarnation == before_incarnation)
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
    if not mihomo_expected_hash and phase in {"transition_applied", "xray_applied"}:
        validation = staged_generation_validation = data.get("staged_generation") or {}
        # The transition digest is recorded in the native validation metadata.
        mihomo_expected_hash = ((validation.get("native_validation") or {}).get("transition") or {}).get("candidate_sha256")
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
    atomic_write_text(checkpoint_path, json.dumps(data, sort_keys=True))
    checkpoint_path.chmod(0o600)
    _fsync_directory(checkpoint_path.parent)
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
    if ok:
        checkpoint_path.unlink(missing_ok=True)
        _fsync_directory(checkpoint_path.parent)
    else:
        _update_xray_generation_checkpoint(checkpoint_path, phase="restore_failed")
    return {"ok": ok, "recovered": "restored_last_good", "xray_reload": xray_restore.ok,
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
        atomic_write_text(checkpoint_path, json.dumps(checkpoint, sort_keys=True))
        checkpoint_path.chmod(0o600)
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
) -> dict[str, Any]:
    adapter = _xray_adapter()
    checkpoint_path = _xray_generation_checkpoint_path(adapter)
    if checkpoint_path.exists():
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
            checkpoint_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint_data["selection_operation_id"] = checkpoint_data.get("generation_id")
            atomic_write_text(checkpoint_path, json.dumps(checkpoint_data, sort_keys=True))
            checkpoint_path.chmod(0o600)
            _fsync_directory(checkpoint_path.parent)
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
