from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_json
from fwrouter_api.services.mihomo_config_paths import (
    _resolved_applied_manifest_path,
    _resolved_base_config_path,
    _resolved_candidate_config_path,
    _resolved_contours_path,
)

FINGERPRINT_VERSION = 1
STATE_FILE_NAME = "reconcile-state.json"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_hash(path: str | Path) -> str | None:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _state_path() -> Path:
    return Path(_resolved_candidate_config_path()).with_name(STATE_FILE_NAME)


def _load_state() -> dict[str, Any] | None:
    path = _state_path()
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _query_rows(sql: str) -> list[dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(sql).fetchall()
    return [dict(row) for row in rows]


def _table_fingerprint() -> dict[str, Any]:
    return {
        "servers": _query_rows(
            """
            SELECT server_id, server_name, provider_name, country_code, region,
                   raw_json, inventory_state, updated_at
            FROM servers
            ORDER BY server_id
            """
        ),
        "server_preferences": _query_rows(
            """
            SELECT server_id, vpn_auto, vpn_auto_priority, global_list,
                   remembered_until, manually_deleted_at, updated_at
            FROM server_preferences
            ORDER BY server_id
            """
        ),
        "server_custom_https_proxy": _query_rows(
            """
            SELECT server_id, host, port, username, password, tls, sni,
                   skip_cert_verify, path, proxy_type, updated_at
            FROM server_custom_https_proxy
            ORDER BY server_id
            """
        ),
        "subject_routes": _query_rows(
            """
            SELECT s.subject_id, s.subject_type, s.is_active, s.is_deleted,
                   l.ip_address AS lan_ip,
                   t.tailscale_ip AS tailscale_ip,
                   d.ip_address AS docker_ip,
                   o.selected_server_id,
                   o.selected_until,
                   o.updated_at AS override_updated_at
            FROM subjects AS s
            LEFT JOIN subject_lan AS l ON l.subject_id = s.subject_id
            LEFT JOIN subject_tailscale AS t ON t.subject_id = s.subject_id
            LEFT JOIN subject_docker AS d ON d.subject_id = s.subject_id
            LEFT JOIN subject_server_overrides AS o ON o.subject_id = s.subject_id
            ORDER BY s.subject_id
            """
        ),
        "subject_user_overrides": _query_rows(
            """
            SELECT subject_id, override_mode, override_until, updated_at
            FROM subject_user_overrides
            ORDER BY subject_id
            """
        ),
        "subject_xray": _query_rows(
            """
            SELECT subject_id, client_id, client_uuid, email, subscription_path,
                   enabled, updated_at
            FROM subject_xray
            ORDER BY subject_id
            """
        ),
        "routing_global_state": _query_rows(
            """
            SELECT id, desired_mode, applied_mode, selective_default, server_mode,
                   desired_fixed_server_id, applied_fixed_server_id,
                   active_auto_server_id, fixed_server_until, updated_at
            FROM routing_global_state
            ORDER BY id
            """
        ),
        "rules_state": _query_rows(
            """
            SELECT id, selective_default, effective_json_path, metadata_path,
                   last_success_at, updated_at
            FROM rules_state
            ORDER BY id
            """
        ),
        "modules": _query_rows(
            """
            SELECT module_name, desired_state, runtime_state, apply_state,
                   lifecycle_mode, updated_at
            FROM modules
            WHERE module_name IN ('vpn', 'xray')
            ORDER BY module_name
            """
        ),
    }


def _input_files_fingerprint() -> dict[str, Any]:
    paths = get_settings().paths
    effective_rules_path = paths.generated_dir / "rules" / "effective-rules.json"
    last_good_effective_rules_path = paths.state_dir / "last-good" / "rules" / "effective-rules.json"
    return {
        "effective_rules": _file_hash(effective_rules_path),
        "last_good_effective_rules": _file_hash(last_good_effective_rules_path),
        "applied_manifest": _file_hash(_resolved_applied_manifest_path()),
        "contours": _file_hash(_resolved_contours_path()),
    }


def _source_fingerprint() -> dict[str, Any]:
    service_dir = Path(__file__).resolve().parent
    files = [
        "mihomo_config.py",
        "mihomo_config_inbounds.py",
        "mihomo_config_paths.py",
        "mihomo_config_validation.py",
        "mihomo_reconcile.py",
        "mihomo_reconcile_fingerprint.py",
        "xray_bindings.py",
        "xray_handoff.py",
        "xray_subscription_service.py",
    ]
    return {name: _file_hash(service_dir / name) for name in files}


def current_mihomo_input_fingerprint(routing: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "version": FINGERPRINT_VERSION,
        "routing": routing if isinstance(routing, dict) else None,
        "tables": _table_fingerprint(),
        "files": _input_files_fingerprint(),
        "source": _source_fingerprint(),
    }
    return {
        "version": FINGERPRINT_VERSION,
        "hash": _json_hash(payload),
        "payload": payload,
    }


def active_config_hash() -> str | None:
    return _file_hash(_resolved_base_config_path())


def mihomo_input_unchanged(fingerprint: dict[str, Any]) -> bool:
    state = _load_state()
    if not isinstance(state, dict):
        return False
    if state.get("fingerprint_hash") != fingerprint.get("hash"):
        return False
    active_hash = active_config_hash()
    if not active_hash:
        return False
    return state.get("active_config_hash") == active_hash


def write_mihomo_reconcile_fingerprint_state(
    *,
    fingerprint: dict[str, Any],
    result: dict[str, Any],
) -> None:
    active_hash = active_config_hash()
    if not active_hash:
        return
    data = {
        "version": FINGERPRINT_VERSION,
        "fingerprint_hash": fingerprint.get("hash"),
        "active_config_hash": active_hash,
        "candidate_config_hash": _file_hash(_resolved_candidate_config_path()),
        "updated_at": _utc_timestamp(),
        "reconcile_reason": result.get("reconcile_reason"),
        "reconcile_action": result.get("reconcile_action"),
    }
    atomic_write_json(_state_path(), data)
