from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_json
from fwrouter_api.services.core_bypass import get_core_bypass_state
from fwrouter_api.services.dataplane_global import read_effective_rules_artifact
from fwrouter_api.services.dataplane_nft import utc_timestamp
from fwrouter_api.services.routing_manifest import build_dataplane_manifest_from_state
from fwrouter_api.services.servers import get_routing_global_state
from fwrouter_api.services.subject_policy import (
    _load_active_server_overrides,
    _load_active_user_overrides,
    enrich_subject_with_effective_state,
)
from fwrouter_api.services.subject_taxonomy import TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES
from fwrouter_api.services.subjects import list_subjects


GLOBAL_PROFILE_SCHEMA_VERSION = 1
GLOBAL_PROFILE_MODES = ("direct", "selective", "vpn")


def _profiles_dir() -> Path:
    return get_settings().paths.generated_dir / "dataplane" / "profiles"


def _profile_path(mode: str) -> Path:
    return _profiles_dir() / f"{mode}.json"


def _profile_meta_path(mode: str) -> Path:
    return _profiles_dir() / f"{mode}.meta.json"


def _normalize_profile_routing(routing: dict[str, Any] | None) -> dict[str, Any]:
    state = dict(routing or get_routing_global_state() or {})
    return {
        "selective_default": str(state.get("selective_default") or "direct"),
        "server_mode": str(state.get("server_mode") or "auto"),
        "desired_fixed_server_id": state.get("desired_fixed_server_id"),
        "applied_fixed_server_id": state.get("applied_fixed_server_id"),
        "fixed_server_until": state.get("fixed_server_until"),
        "active_auto_server_id": state.get("active_auto_server_id"),
    }


def _stable_json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rows_digest(rows: list[dict[str, Any]]) -> str:
    return _stable_json_digest(rows)


def _query_digest(sql: str) -> dict[str, Any]:
    with db_session() as connection:
        rows = [dict(row) for row in connection.execute(sql).fetchall()]
    return {"rows_count": len(rows), "sha256": _rows_digest(rows)}


def _subjects_stamp() -> dict[str, Any]:
    # Only fields that affect dataplane materialization belong here. UI labels,
    # traffic timestamps and generic updated_at churn must not invalidate
    # precompiled global profiles.
    return _query_digest(
        """
        SELECT
            s.subject_id,
            s.subject_type,
            s.stable_key,
            s.desired_mode,
            s.applied_mode,
            s.runtime_state,
            s.is_active,
            s.is_deleted,
            s.deleted_at,
            l.mac_address AS lan_mac_address,
            l.ip_address AS lan_ip_address,
            t.node_id AS tailscale_node_id,
            t.tailscale_ip AS tailscale_ip,
            t.online AS tailscale_online,
            x.client_id AS xray_client_id,
            x.client_uuid AS xray_client_uuid,
            x.email AS xray_email,
            x.enabled AS xray_enabled,
            d.ip_address AS docker_ip_address,
            d.network_name AS docker_network_name,
            h.listen_proto AS host_listen_proto,
            h.listen_port AS host_listen_port,
            f.component_name AS fwrouter_component_name
        FROM subjects s
        LEFT JOIN subject_lan l ON l.subject_id = s.subject_id
        LEFT JOIN subject_tailscale t ON t.subject_id = s.subject_id
        LEFT JOIN subject_xray x ON x.subject_id = s.subject_id
        LEFT JOIN subject_docker d ON d.subject_id = s.subject_id
        LEFT JOIN subject_host h ON h.subject_id = s.subject_id
        LEFT JOIN subject_fwrouter f ON f.subject_id = s.subject_id
        ORDER BY s.subject_id
        """
    )


def _subject_user_overrides_stamp() -> dict[str, Any]:
    return _query_digest(
        """
        SELECT subject_id, override_mode, override_until
        FROM subject_user_overrides
        ORDER BY subject_id
        """
    )


def _subject_server_overrides_stamp() -> dict[str, Any]:
    # apply_state/error fields are status bookkeeping updated after applies.
    # Only selected target content changes the manifest.
    return _query_digest(
        """
        SELECT subject_id, selected_server_id, selected_until
        FROM subject_server_overrides
        ORDER BY subject_id
        """
    )


def _rules_digest(value: dict[str, Any] | None) -> str | None:
    if not isinstance(value, dict):
        return None
    return _stable_json_digest(value)


def build_global_profile_source_stamp(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    effective_rules = read_effective_rules_artifact()
    core_bypass = get_core_bypass_state()
    return {
        "schema_version": GLOBAL_PROFILE_SCHEMA_VERSION,
        "routing": _normalize_profile_routing(routing),
        "subjects": _subjects_stamp(),
        "subject_user_overrides": _subject_user_overrides_stamp(),
        "subject_server_overrides": _subject_server_overrides_stamp(),
        "rules_effective_sha256": _rules_digest(effective_rules),
        "core_bypass": {
            "enabled": bool(core_bypass.get("enabled")),
            "reason": core_bypass.get("reason"),
            "status": core_bypass.get("status"),
        },
    }


def _future_routing(base_routing: dict[str, Any], *, mode: str) -> dict[str, Any]:
    future = dict(base_routing)
    future["desired_mode"] = mode
    future["applied_mode"] = mode
    future["apply_state"] = "compiled"
    future["error_code"] = None
    future["error_message"] = None
    return future


def _subject_runtime_statuses(subjects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for subject in subjects:
        scoped_runtime = ((subject.get("effective_state") or {}).get("scoped_runtime") or {})
        statuses.append(
            {
                "subject_id": str(subject.get("subject_id") or ""),
                "scoped_runtime_status": str(scoped_runtime.get("status") or "unknown"),
            }
        )
    return statuses


def _affected_subject_ids(subjects: list[dict[str, Any]]) -> list[str]:
    affected: list[str] = []
    for subject in subjects:
        subject_type = str(subject.get("subject_type") or "")
        if subject_type not in TRANSPARENT_INGRESS_CLIENT_SUBJECT_TYPES and subject_type != "tailscale":
            continue
        if str(subject.get("desired_mode") or "") != "global":
            continue
        effective = subject.get("effective_state") or {}
        if str(effective.get("mode_source") or "") != "global":
            continue
        affected.append(str(subject.get("subject_id") or ""))
    return [item for item in affected if item]


def compile_global_mode_profile(mode: str, *, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in GLOBAL_PROFILE_MODES:
        raise ValueError(f"Unsupported global profile mode: {mode}")

    base_routing = dict(routing or get_routing_global_state() or {})
    source_stamp = build_global_profile_source_stamp(routing=base_routing)
    future_routing = _future_routing(base_routing, mode=normalized_mode)
    effective_rules = read_effective_rules_artifact()
    core_bypass = get_core_bypass_state()

    subjects = list_subjects(
        include_deleted=False,
        include_detail=True,
        limit=1000,
    )
    subject_ids = [str(subject.get("subject_id") or "") for subject in subjects]
    user_overrides = _load_active_user_overrides(subject_ids)
    server_overrides = _load_active_server_overrides(subject_ids)
    enriched_subjects = [
        enrich_subject_with_effective_state(
            subject,
            routing=future_routing,
            user_override=user_overrides.get(str(subject.get("subject_id") or "")),
            server_override=server_overrides.get(str(subject.get("subject_id") or "")),
        )
        for subject in subjects
    ]
    affected_subject_ids = _affected_subject_ids(enriched_subjects)

    manifest = build_dataplane_manifest_from_state(
        plan_id=f"precompiled-{normalized_mode}",
        reason=f"precompiled_global_{normalized_mode}",
        routing=future_routing,
        subjects=enriched_subjects,
        input_data={"intent": "precompiled_global_profile", "mode": normalized_mode},
        extra={
            "core_bypass": core_bypass,
            "rules_effective": effective_rules,
        },
    )

    payload = {
        "schema_version": GLOBAL_PROFILE_SCHEMA_VERSION,
        "target_mode": normalized_mode,
        "compiled_at": utc_timestamp(),
        "source_stamp": source_stamp,
        "affected_subject_ids": affected_subject_ids,
        "subject_runtime_statuses": _subject_runtime_statuses(enriched_subjects),
        "manifest": manifest,
    }
    atomic_write_json(_profile_path(normalized_mode), payload)
    atomic_write_json(
        _profile_meta_path(normalized_mode),
        {
            "schema_version": GLOBAL_PROFILE_SCHEMA_VERSION,
            "target_mode": normalized_mode,
            "compiled_at": payload["compiled_at"],
            "source_stamp": source_stamp,
            "profile_path": str(_profile_path(normalized_mode)),
        },
    )
    return payload


def compile_all_global_mode_profiles(*, routing: dict[str, Any] | None = None) -> dict[str, Any]:
    compiled: dict[str, Any] = {}
    for mode in GLOBAL_PROFILE_MODES:
        compiled[mode] = compile_global_mode_profile(mode, routing=routing)
    return compiled


def load_precompiled_global_mode_profile(
    mode: str,
    *,
    routing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in GLOBAL_PROFILE_MODES:
        return None

    path = _profile_path(normalized_mode)
    if not path.exists():
        return None

    meta_path = _profile_meta_path(normalized_mode)
    expected_source_stamp = build_global_profile_source_stamp(routing=routing)
    if meta_path.exists():
        try:
            meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(meta_payload, dict):
            return None
        if int(meta_payload.get("schema_version") or 0) != GLOBAL_PROFILE_SCHEMA_VERSION:
            return None
        if str(meta_payload.get("target_mode") or "") != normalized_mode:
            return None
        if meta_payload.get("source_stamp") != expected_source_stamp:
            return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("schema_version") or 0) != GLOBAL_PROFILE_SCHEMA_VERSION:
        return None
    if str(payload.get("target_mode") or "") != normalized_mode:
        return None
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        return None
    if payload.get("source_stamp") != expected_source_stamp:
        return None
    return payload


def materialize_precompiled_manifest(
    payload: dict[str, Any],
    *,
    plan_id: str,
    reason: str,
    input_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = dict(payload.get("manifest") or {})
    manifest["plan_id"] = plan_id
    manifest["reason"] = reason
    manifest["generated_at"] = utc_timestamp()
    manifest["input"] = input_data or {}
    return manifest
