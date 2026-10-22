"""Bounded read-only native state proof for the supported managed UI scope."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
from typing import Any
from functools import wraps


_MAX_BACKEND_FILES = 10_000
_MAX_BACKEND_BYTES = 512 * 1024 * 1024
_EXPECTED_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ACTIVE_JOB_STATES = ("queued", "running")
_CORE_NATIVE_PROOF_NAME = "applied-native-proof.json"
_DYNAMIC_NFT_SET_ELEMENTS = {"dns_direct_ipv4", "dns_vpn_ipv4"}
_SAFE_REASON_CODES = {
    "BACKEND_FILE_CHANGED", "BACKEND_FILE_INVALID", "BACKEND_FILE_TOO_LARGE",
    "BACKEND_MANIFEST_EMPTY", "BACKEND_MANIFEST_TOO_LARGE", "BACKEND_ROOT_UNAVAILABLE",
    "BACKEND_SYMLINK_DIRECTORY", "CORE_APPLIED_MANIFEST_MISMATCH", "CORE_IP_POLICY_MISMATCH",
    "CORE_IP_READBACK_INVALID", "CORE_IP_READBACK_UNAVAILABLE", "CORE_NATIVE_OWNER_RECEIPT_UNAVAILABLE",
    "CORE_NATIVE_READBACK_UNAVAILABLE", "CORE_NATIVE_RULES_MISMATCH", "CORE_NATIVE_STATE_MISMATCH",
    "CORE_NFT_READBACK_CHAIN_MISSING", "CORE_NFT_READBACK_INCOMPLETE", "CORE_NFT_READBACK_INVALID",
    "CORE_NFT_READBACK_UNAVAILABLE", "CORE_NFT_READBACK_UNEXPECTED_TABLE",
    "CORE_NFT_READBACK_UNSUPPORTED_OBJECT", "CORE_NFT_READBACK_UNSUPPORTED_RULE",
    "CORE_RUNTIME_CONTEXT_CHANGED", "MIHOMO_DURABLE_CONFIG_PROJECTION_MISMATCH",
    "MIHOMO_NATIVE_POLICY_MISMATCH", "MIHOMO_NATIVE_RULES_UNAVAILABLE", "MIHOMO_OWNER_RECEIPT_UNAVAILABLE",
    "MIHOMO_READBACK_FENCE_CHANGED", "MIHOMO_RUNTIME_ID_UNAVAILABLE",
    "MIHOMO_RUNTIME_IMAGE_REFERENCE_MISMATCH", "MIHOMO_RUNTIME_VERSION_MISMATCH",
    "MIHOMO_SELECTION_TARGET_MISMATCH", "NATIVE_ARTIFACT_CHANGED", "NATIVE_ARTIFACT_INVALID",
    "NATIVE_ARTIFACT_TOO_LARGE", "NATIVE_RUNTIME_READBACK_FAILED", "XRAY_ACTIVE_BINDINGS_MISMATCH",
    "XRAY_BINDINGS_ARTIFACT_INVALID", "XRAY_DURABLE_BINDING_PROJECTION_MISMATCH",
    "XRAY_DURABLE_CLIENT_MODE_PROJECTION_MISMATCH", "XRAY_DURABLE_IDENTITIES_INVALID",
    "XRAY_LOADED_IDENTITIES_MISMATCH", "XRAY_MOUNTED_CONFIG_MISMATCH", "XRAY_READBACK_FENCE_CHANGED",
    "XRAY_RUNTIME_ID_UNAVAILABLE", "XRAY_RUNTIME_VERSION_UNAVAILABLE",
    "XRAY_RUNTIME_IMAGE_REFERENCE_MISMATCH", "MIHOMO_RUNTIME_IMAGE_REFERENCE_MISMATCH",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _safe_failure_reason(exc: Exception) -> str:
    candidate = str(exc)
    return candidate if candidate in _SAFE_REASON_CODES else "native_readback_unavailable"


def _normalize_nft_expression(value: Any) -> Any:
    """Drop only volatile counter values while retaining the rule expression."""
    if isinstance(value, list):
        return [_normalize_nft_expression(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key == "counter" and isinstance(item, dict):
            normalized[key] = {name: data for name, data in item.items() if name not in {"packets", "bytes"}}
        else:
            normalized[key] = _normalize_nft_expression(item)
    return normalized


def canonical_nft_table(payload: Any) -> dict[str, Any]:
    """Normalize `nft -j -s list table` without discarding policy semantics.

    Handles and packet/byte counter values are volatile. Elements are ignored
    only for the two explicitly runtime-populated DNS timeout sets; their
    declaration (type, flags and timeout) remains part of the proof.
    """
    entries = payload.get("nftables") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("CORE_NFT_READBACK_INVALID")
    table: list[dict[str, Any]] = []
    chains: list[dict[str, Any]] = []
    sets: list[dict[str, Any]] = []
    elements: list[dict[str, Any]] = []
    rules_by_chain: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("CORE_NFT_READBACK_INVALID")
        if "metainfo" in entry:
            continue
        recognized = [name for name in ("table", "chain", "set", "element", "rule") if name in entry]
        if len(recognized) != 1:
            raise ValueError("CORE_NFT_READBACK_UNSUPPORTED_OBJECT")
        kind = recognized[0]
        row = entry[kind]
        expected_table_name = row.get("name") if kind == "table" and isinstance(row, dict) else row.get("table") if isinstance(row, dict) else None
        if not isinstance(row, dict) or row.get("family") != "inet" or expected_table_name != "fwrouter_v2":
            raise ValueError("CORE_NFT_READBACK_UNEXPECTED_TABLE")
        normalized = {key: _normalize_nft_expression(value) for key, value in row.items() if key != "handle"}
        if kind == "element":
            name = str(row.get("name") or "")
            if name in _DYNAMIC_NFT_SET_ELEMENTS:
                continue
            elements.append(normalized)
        elif kind == "rule":
            chain = str(row.get("chain") or "")
            if not chain or not isinstance(row.get("expr"), list):
                raise ValueError("CORE_NFT_READBACK_UNSUPPORTED_RULE")
            rules_by_chain.setdefault(chain, []).append(normalized)
        elif kind == "table":
            table.append(normalized)
        elif kind == "chain":
            chains.append(normalized)
        elif kind == "set":
            if str(row.get("name") or "") in _DYNAMIC_NFT_SET_ELEMENTS and "elem" in normalized:
                normalized.pop("elem", None)
            sets.append(normalized)
    if len(table) != 1 or not chains:
        raise ValueError("CORE_NFT_READBACK_INCOMPLETE")
    chain_names = {str(item.get("name") or "") for item in chains}
    if not set(rules_by_chain).issubset(chain_names):
        raise ValueError("CORE_NFT_READBACK_CHAIN_MISSING")
    return {
        "table": table[0],
        "chains": sorted(chains, key=lambda item: str(item.get("name") or "")),
        "sets": sorted(sets, key=lambda item: str(item.get("name") or "")),
        "elements": sorted(elements, key=lambda item: (str(item.get("name") or ""), _canonical_json(item))),
        # Rule array order within each chain is traffic-policy precedence.
        "rules": {name: rules_by_chain[name] for name in sorted(rules_by_chain)},
    }


def canonical_nft_table_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(canonical_nft_table(payload))).hexdigest()


def read_live_nft_semantic_sha256() -> str:
    completed = subprocess.run(
        ["nft", "-j", "-s", "list", "table", "inet", "fwrouter_v2"],
        check=False, capture_output=True, text=True, timeout=4,
    )
    if completed.returncode != 0:
        raise ValueError("CORE_NFT_READBACK_UNAVAILABLE")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("CORE_NFT_READBACK_INVALID") from exc
    return canonical_nft_table_sha256(payload)


def core_native_proof_path() -> Path:
    from fwrouter_api.core.config import get_settings

    return get_settings().paths.generated_dir / "dataplane" / _CORE_NATIVE_PROOF_NAME


def write_core_native_apply_receipt(
    *, manifest: dict[str, Any], artifact_paths: dict[str, Any], core_intent_revision: int | None,
) -> bool:
    """Write a native table receipt after the existing apply owner verified replacement."""
    try:
        from fwrouter_api.services.artifacts import atomic_write_json

        apply_id = manifest.get("plan_id")
        candidate_path = Path(str(artifact_paths.get("candidate_nft_path") or ""))
        applied_path = Path(str(artifact_paths.get("applied_nft_path") or ""))
        applied_manifest_path = Path(str(artifact_paths.get("applied_manifest_path") or ""))
        if (not isinstance(apply_id, str) or not apply_id or type(core_intent_revision) is not int
                or core_intent_revision < 0 or not candidate_path.is_file() or not applied_path.is_file()
                or not applied_manifest_path.is_file()):
            return False
        candidate_sha = _hash_regular_file(candidate_path, maximum_bytes=_MAX_BACKEND_BYTES)
        applied_sha = _hash_regular_file(applied_path, maximum_bytes=_MAX_BACKEND_BYTES)
        manifest_sha = _hash_regular_file(applied_manifest_path)
        if candidate_sha != applied_sha:
            return False
        semantic_sha = read_live_nft_semantic_sha256()
        ip_semantic_sha = _core_ip_facts()["semantic_sha256"]
        # Re-read after native capture so an artifact replacement cannot be
        # mistaken for the owner input that was just applied.
        if (_hash_regular_file(candidate_path, maximum_bytes=_MAX_BACKEND_BYTES) != candidate_sha
                or _hash_regular_file(applied_path, maximum_bytes=_MAX_BACKEND_BYTES) != applied_sha
                or _hash_regular_file(applied_manifest_path) != manifest_sha):
            return False
        if (read_live_nft_semantic_sha256() != semantic_sha
                or _core_ip_facts()["semantic_sha256"] != ip_semantic_sha):
            return False
        receipt = {
            "schema": 1,
            "apply_id": apply_id,
            "core_intent_revision": core_intent_revision,
            "candidate_nft_sha256": candidate_sha,
            "applied_manifest_sha256": manifest_sha,
            "native_semantics_sha256": semantic_sha,
            "native_ip_semantics_sha256": ip_semantic_sha,
            "normalization": "nft-table-v1-counter-values-and-dns-timeout-elements-only",
        }
        atomic_write_json(core_native_proof_path(), receipt)
        return True
    except Exception:
        # This receipt is optional evidence added after the existing apply
        # owner has succeeded. It must never turn that established success
        # into an apply failure; the strict reader will report it incomplete.
        return False


def _excluded_backend_path(path: PurePosixPath) -> bool:
    # Keep this byte-for-byte compatible with installer.controlled_backend_lifecycle._excluded.
    return any(
        part in {".env", ".venv", ".pytest_cache", "__pycache__", ".git"}
        or part.startswith(".env.")
        or part.endswith((".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".bak", ".tar.zst"))
        or ".bak-" in part
        for part in path.parts
    )


def backend_manifest(root: Path) -> list[dict[str, Any]]:
    """Return the installed backend content manifest used by the rollout owner."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("BACKEND_ROOT_UNAVAILABLE")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        relative_dir = PurePosixPath(base.relative_to(root).as_posix())
        kept_dirs: list[str] = []
        for name in dirs:
            relative = relative_dir / name
            if _excluded_backend_path(relative):
                continue
            if (base / name).is_symlink():
                raise ValueError("BACKEND_SYMLINK_DIRECTORY")
            kept_dirs.append(name)
        dirs[:] = sorted(kept_dirs)
        for name in sorted(files):
            relative = relative_dir / name
            if _excluded_backend_path(relative):
                continue
            path = base / name
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_BACKEND_BYTES:
                    raise ValueError("BACKEND_FILE_INVALID")
                digest = hashlib.sha256()
                size = 0
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > _MAX_BACKEND_BYTES:
                        raise ValueError("BACKEND_FILE_TOO_LARGE")
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                if (size != before.st_size or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns):
                    raise ValueError("BACKEND_FILE_CHANGED")
            total_bytes += size
            if len(entries) >= _MAX_BACKEND_FILES or total_bytes > _MAX_BACKEND_BYTES:
                raise ValueError("BACKEND_MANIFEST_TOO_LARGE")
            entries.append({"path": relative.as_posix(), "size": size, "sha256": digest.hexdigest()})
    entries.sort(key=lambda item: item["path"])
    if not entries:
        raise ValueError("BACKEND_MANIFEST_EMPTY")
    return entries


def backend_manifest_sha256(root: Path) -> str:
    return hashlib.sha256(_canonical_json(backend_manifest(root))).hexdigest()


def _backend_root() -> Path:
    # Attest the package tree that supplied this executing module. An
    # environment-selected directory could otherwise make the CLI report a
    # matching manifest for a different backend than the code it is running.
    return Path(__file__).resolve(strict=True).parents[2]


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _hash_regular_file(path: Path, *, maximum_bytes: int = 16 * 1024 * 1024) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise ValueError("NATIVE_ARTIFACT_INVALID")
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(64 * 1024):
            size += len(chunk)
            if size > maximum_bytes:
                raise ValueError("NATIVE_ARTIFACT_TOO_LARGE")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (size != before.st_size or before.st_dev != after.st_dev or before.st_ino != after.st_ino
                or before.st_mtime_ns != after.st_mtime_ns):
            raise ValueError("NATIVE_ARTIFACT_CHANGED")
    return digest.hexdigest()


def _read_regular_file(path: Path, *, maximum_bytes: int = 16 * 1024 * 1024) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise ValueError("NATIVE_ARTIFACT_INVALID")
        raw = stream.read(maximum_bytes + 1)
        after = os.fstat(stream.fileno())
        if (len(raw) != before.st_size or len(raw) > maximum_bytes or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino or before.st_mtime_ns != after.st_mtime_ns):
            raise ValueError("NATIVE_ARTIFACT_CHANGED")
    return raw


def _checkpoint_snapshot() -> dict[str, Any]:
    from fwrouter_api.adapters.xray_common import _default_xray_config_path

    path = Path(_default_xray_config_path()).parent / ".generation" / "generation-checkpoint.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"present": False, "sha256": None}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return {"present": True, "valid": False, "sha256": None}
    try:
        return {"present": True, "valid": True, "sha256": _hash_regular_file(path, maximum_bytes=1024 * 1024)}
    except (OSError, ValueError):
        return {"present": True, "valid": False, "sha256": None}


def _db_snapshot(path: Path) -> dict[str, Any]:
    from fwrouter_api.db.schema_state import inspect_database_schema
    from fwrouter_api.services.core_intent import read_core_intent_revision
    from fwrouter_api.services.subscription import _read_subscription_native_generation, _subscription_inventory_digest
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
    from fwrouter_api.services.xray_subscription_service import _legacy_admission_source_fingerprint

    connection = _open_readonly_database(path)
    try:
        schema = inspect_database_schema(connection)
        integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        inventory_digest, inventory_count = _subscription_inventory_digest(connection)
        generation = _read_subscription_native_generation(connection)
        selection = read_selection_fence(connection)
        core_revision = read_core_intent_revision(connection)
        source_fingerprint = _legacy_admission_source_fingerprint(connection)
        latest_apply_row = connection.execute(
            "SELECT apply_id, status, core_intent_revision FROM apply_versions "
            "ORDER BY created_at DESC, apply_id DESC LIMIT 1",
        ).fetchone()
        latest_apply = dict(latest_apply_row) if latest_apply_row is not None else None
        routing_row = connection.execute(
            "SELECT desired_mode, applied_mode, apply_state, selective_default, server_mode, "
            "desired_fixed_server_id, applied_fixed_server_id, active_auto_server_id "
            "FROM routing_global_state WHERE id=1",
        ).fetchone()
        routing = dict(routing_row) if routing_row is not None else None
        modules = [dict(row) for row in connection.execute(
            "SELECT module_name, desired_state, lifecycle_mode, apply_state FROM modules "
            "WHERE module_name IN ('core','vpn','xray') ORDER BY module_name",
        )]
        active_job_rows = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?)", _ACTIVE_JOB_STATES,
        ).fetchone()
        active_jobs = int(active_job_rows[0]) if active_job_rows else 0
        identities = [dict(row) for row in connection.execute(
            "SELECT json_extract(metadata_json, '$.detail.client_uuid') AS client_uuid, "
            "json_extract(metadata_json, '$.detail.client_id') AS client_id, "
            "json_extract(metadata_json, '$.detail.email') AS email "
            "FROM subjects WHERE implementation_kind='xray' AND is_active=1 AND is_deleted=0 "
            "AND COALESCE(json_extract(metadata_json, '$.detail.enabled'), 1)=1 ORDER BY subject_id",
        )]
        return {
            "schema": schema,
            "integrity": integrity_rows,
            "foreign_key_violations": len(foreign_keys),
            "inventory_digest": inventory_digest,
            "inventory_count": inventory_count,
            "generation": generation,
            "selection": selection,
            "core_revision": core_revision,
            "source_fingerprint": source_fingerprint,
            "latest_apply": latest_apply,
            "routing": routing,
            "modules": modules,
            "active_jobs": active_jobs,
            "xray_identities": identities,
        }
    finally:
        connection.rollback()
        connection.close()


def _docker_readonly(arguments: list[str], timeout: float = 4.0) -> str:
    env = dict(os.environ)
    env.pop("DOCKER_CONFIG", None)
    env["HOME"] = "/proc"
    completed = subprocess.run(
        ["docker", *arguments], check=False, capture_output=True, text=True, timeout=timeout, env=env,
    )
    if completed.returncode != 0:
        raise ValueError("NATIVE_RUNTIME_READBACK_FAILED")
    return completed.stdout.strip()


def _mihomo_runtime_identity(adapter: Any | None = None) -> dict[str, str]:
    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
    from fwrouter_api.services.mihomo_runtime import MIHOMO_COMPOSE_FILE, MIHOMO_COMPOSE_SERVICE

    adapter = adapter or DEFAULT_MIHOMO_ADAPTER
    listed = _docker_readonly(["compose", "-f", str(MIHOMO_COMPOSE_FILE), "ps", "-q", MIHOMO_COMPOSE_SERVICE])
    container_id = listed.splitlines()[-1] if listed else ""
    if not re.fullmatch(r"[0-9a-fA-F]{12,64}", container_id):
        raise ValueError("MIHOMO_RUNTIME_ID_UNAVAILABLE")
    details = _docker_readonly([
        "inspect", "--format", "{{.Id}}|{{.Image}}|{{.Config.Image}}|{{.State.Running}}|{{.State.StartedAt}}",
        container_id,
    ])
    parts = details.split("|", 4)
    if (len(parts) != 5 or parts[3] != "true" or not parts[4] or not parts[0].startswith(container_id)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", parts[1])):
        raise ValueError("MIHOMO_RUNTIME_ID_UNAVAILABLE")
    from fwrouter_api.services.subscription_pipeline import MIHOMO_IMAGE
    if parts[2] != MIHOMO_IMAGE:
        raise ValueError("MIHOMO_RUNTIME_IMAGE_REFERENCE_MISMATCH")
    configured_images = set(_docker_readonly([
        "compose", "-f", str(MIHOMO_COMPOSE_FILE), "config", "--images",
    ]).splitlines())
    if parts[2] not in configured_images:
        raise ValueError("MIHOMO_RUNTIME_IMAGE_REFERENCE_MISMATCH")
    version = adapter._get_json("/version", timeout_seconds=2.0).get("version")
    if not isinstance(version, str) or "v1.19.31" not in version:
        raise ValueError("MIHOMO_RUNTIME_VERSION_MISMATCH")
    return {
        "image_digest": parts[1],
        "image_reference_sha256": hashlib.sha256(parts[2].encode("utf-8")).hexdigest(),
        "version_sha256": hashlib.sha256(version.encode("utf-8")).hexdigest(),
        "incarnation_sha256": hashlib.sha256((details + "|" + version).encode("utf-8")).hexdigest(),
    }


def _xray_runtime_identity(adapter: Any) -> dict[str, str]:
    listed = adapter._run("runtime_container_id")
    ids = str(listed.details.get("stdout") or "").strip().splitlines()
    if not listed.ok or not ids or not re.fullmatch(r"[0-9a-fA-F]{12,64}", ids[-1]):
        raise ValueError("XRAY_RUNTIME_ID_UNAVAILABLE")
    inspected = _docker_readonly([
        "inspect", "--format", "{{.Id}}|{{.Image}}|{{.Config.Image}}|{{.State.Running}}|{{.State.StartedAt}}",
        ids[-1],
    ])
    parts = inspected.split("|", 4)
    if (len(parts) != 5 or parts[3] != "true" or not parts[4] or not parts[0].startswith(ids[-1])
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", parts[1])):
        raise ValueError("XRAY_RUNTIME_ID_UNAVAILABLE")
    configured_images = set(_docker_readonly([
        "compose", "-f", str(adapter.compose_path), "config", "--images",
    ]).splitlines())
    if not parts[2] or parts[2] not in configured_images:
        raise ValueError("XRAY_RUNTIME_IMAGE_REFERENCE_MISMATCH")
    version = _docker_readonly(["exec", ids[-1], "xray", "version"])
    if not version.strip():
        raise ValueError("XRAY_RUNTIME_VERSION_UNAVAILABLE")
    return {
        "image_digest": parts[1],
        "image_reference_sha256": hashlib.sha256(parts[2].encode("utf-8")).hexdigest(),
        "version_sha256": hashlib.sha256(version.encode("utf-8")).hexdigest(),
        "incarnation_sha256": hashlib.sha256((adapter.get_runtime_incarnation() + "|" + inspected + "|" + version).encode("utf-8")).hexdigest(),
    }


def _xray_readback(
    db: dict[str, Any], *, adapter: Any | None = None,
    runtime_identity_reader: Any | None = None,
) -> dict[str, Any]:
    from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER
    from fwrouter_api.services.xray_bindings import (
        _safe_binding_for_state, collect_xray_client_mode_directives, collect_xray_runtime_bindings,
    )
    from fwrouter_api.services.xray_materialize import _verify_active_config_bindings, _verify_active_config_client_modes
    from fwrouter_api.services.xray_runtime_state import _xray_bindings_path

    adapter = adapter or DEFAULT_XRAY_ADAPTER
    config_sha = _hash_regular_file(Path(adapter.config_path))
    identity_reader = runtime_identity_reader or (lambda: _xray_runtime_identity(adapter))
    runtime_identity_before = identity_reader()
    mounted_sha = adapter.get_runtime_config_sha256()
    loaded = sorted((str(left), str(right)) for left, right in adapter.list_loaded_client_identities())
    binding_path = Path(_xray_bindings_path())
    raw = _read_regular_file(binding_path)
    bindings_document = json.loads(raw.decode("utf-8"))
    if not isinstance(bindings_document, dict):
        raise ValueError("XRAY_BINDINGS_ARTIFACT_INVALID")
    bindings = bindings_document.get("bindings")
    modes = bindings_document.get("client_modes")
    if not isinstance(bindings, list) or not isinstance(modes, list):
        raise ValueError("XRAY_BINDINGS_ARTIFACT_INVALID")
    if any(not isinstance(row, dict) for row in [*bindings, *modes]):
        raise ValueError("XRAY_BINDINGS_ARTIFACT_INVALID")
    expected_identities = sorted(
        (str(row.get("client_uuid") or row.get("client_id") or "").strip(), str(row.get("email") or "").strip())
        for row in db["xray_identities"]
    )
    if any(not left or not right for left, right in expected_identities) or len(expected_identities) != len(set(expected_identities)):
        raise ValueError("XRAY_DURABLE_IDENTITIES_INVALID")
    expected_bindings = [_safe_binding_for_state(item) for item in collect_xray_runtime_bindings()]
    expected_modes = collect_xray_client_mode_directives()

    def stable_projection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            ({key: value for key, value in row.items() if key not in {"status", "applied_at"}}
             for row in rows if isinstance(row, dict)),
            key=_canonical_json,
        )

    if stable_projection(bindings) != stable_projection(expected_bindings):
        raise ValueError("XRAY_DURABLE_BINDING_PROJECTION_MISMATCH")
    if stable_projection(modes) != stable_projection(expected_modes):
        raise ValueError("XRAY_DURABLE_CLIENT_MODE_PROJECTION_MISMATCH")
    if loaded != expected_identities:
        raise ValueError("XRAY_LOADED_IDENTITIES_MISMATCH")
    binding_proof = _verify_active_config_bindings(
        expected_bindings, expected_client_identities=expected_identities,
    )
    mode_proof = _verify_active_config_client_modes(expected_modes)
    if binding_proof.get("ok") is not True or mode_proof.get("ok") is not True:
        raise ValueError("XRAY_ACTIVE_BINDINGS_MISMATCH")
    runtime_identity_after = identity_reader()
    mounted_after = adapter.get_runtime_config_sha256()
    config_after = _hash_regular_file(Path(adapter.config_path))
    if runtime_identity_before != runtime_identity_after or mounted_sha != mounted_after or config_sha != config_after:
        raise RuntimeError("XRAY_READBACK_FENCE_CHANGED")
    if config_sha != mounted_sha:
        raise ValueError("XRAY_MOUNTED_CONFIG_MISMATCH")
    return {
        "config_matches_mounted": config_sha == mounted_sha,
        "runtime_incarnation_stable": True,
        "identity_count": len(expected_identities),
        "bindings_match": True,
        "modes_match": True,
        "durable_bindings_sha256": hashlib.sha256(_canonical_json(stable_projection(expected_bindings))).hexdigest(),
        "durable_modes_sha256": hashlib.sha256(_canonical_json(stable_projection(expected_modes))).hexdigest(),
        "runtime_image_digest": runtime_identity_before["image_digest"],
        "runtime_image_reference_sha256": runtime_identity_before["image_reference_sha256"],
        "runtime_version_sha256": runtime_identity_before["version_sha256"],
        "config_sha256": config_sha,
        "runtime_config_sha256": mounted_sha,
        "runtime_incarnation_sha256": runtime_identity_before["incarnation_sha256"],
    }


def _mihomo_readback(
    db: dict[str, Any], *, adapter: Any | None = None,
    runtime_identity_reader: Any | None = None,
) -> dict[str, Any]:
    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoHttpAdapter

    adapter = adapter or DEFAULT_MIHOMO_ADAPTER
    config_path = Path(adapter.config_path)
    config_sha = _hash_regular_file(config_path)
    identity_reader = runtime_identity_reader or (lambda: _mihomo_runtime_identity(adapter))
    runtime_identity_before = identity_reader()
    config_text = adapter._config_text()
    parsed = MihomoHttpAdapter._parse_vpn_udp_policy_config(config_text)
    # Import the established routing facade first: `mihomo_config` imports the
    # custom-server projection, which shares this facade's override contracts.
    from fwrouter_api.services import servers
    from fwrouter_api.services import mihomo_config
    from fwrouter_api.services.mihomo_config_status import _configs_equal
    from fwrouter_api.services.mihomo_config_yaml import hash_mihomo_yaml_file
    from fwrouter_api.services.xray_handoff import build_xray_handoff_assignments
    from fwrouter_api.services.xray_bindings import collect_xray_runtime_bindings
    from fwrouter_api.services.mihomo_reconcile_fingerprint import (
        _load_state, current_mihomo_input_fingerprint, mihomo_input_unchanged,
        mihomo_udp_policy_receipt_matches,
    )
    expected_bindings = collect_xray_runtime_bindings()
    expected_handoffs = build_xray_handoff_assignments(expected_bindings)
    routing = servers.get_routing_global_state(expire_ttl=False)
    expected_config = mihomo_config.build_mihomo_config(routing, xray_handoff_assignments=expected_handoffs)
    if not _configs_equal(parsed, expected_config):
        raise ValueError("MIHOMO_DURABLE_CONFIG_PROJECTION_MISMATCH")
    fingerprint = current_mihomo_input_fingerprint(routing)
    if not mihomo_input_unchanged(fingerprint) or not mihomo_udp_policy_receipt_matches():
        raise ValueError("MIHOMO_OWNER_RECEIPT_UNAVAILABLE")
    input_state = _load_state()
    if not isinstance(input_state, dict):
        raise ValueError("MIHOMO_OWNER_RECEIPT_UNAVAILABLE")
    proxies = adapter._proxies(timeout_seconds=2.0)
    rules_payload = adapter._get_json("/rules", timeout_seconds=2.0)
    rules = rules_payload.get("rules")
    if not isinstance(rules, list):
        raise ValueError("MIHOMO_NATIVE_RULES_UNAVAILABLE")
    udp_policy = MihomoHttpAdapter._vpn_udp_policy_readback(
        config_text, proxies, rules, _parsed_config=parsed,
    )
    checks = {
        "native_top_level_rules_match": udp_policy.get("native_top_level_rules_match") is True,
        "selector_membership_matches": udp_policy.get("selector_membership_matches") is True,
        "guarded_rules_match": udp_policy.get("guarded_rules_match") is True,
    }
    if not all(checks.values()):
        raise ValueError("MIHOMO_NATIVE_POLICY_MISMATCH")
    selectors = {
        name: {
            "type": value.get("type"),
            "now": value.get("now"),
            "all": sorted(str(item) for item in value.get("all", []) if isinstance(item, str)),
        }
        for name, value in sorted(proxies.items())
        if isinstance(value, dict) and (name in {"vpn-auto", "vpn-global"} or name.startswith("fwrouter-subject-"))
    }
    selection_match = _mihomo_selection_target_matches(db, expected_config, proxies)
    if not selection_match:
        raise ValueError("MIHOMO_SELECTION_TARGET_MISMATCH")
    runtime_identity_after = identity_reader()
    config_after = _hash_regular_file(config_path)
    if runtime_identity_before != runtime_identity_after or config_sha != config_after:
        raise RuntimeError("MIHOMO_READBACK_FENCE_CHANGED")
    return {
        "config_stable": True,
        "runtime_incarnation_stable": True,
        "config_sha256": hash_mihomo_yaml_file(config_path),
        "semantic_config_sha256": hashlib.sha256(_canonical_json(parsed)).hexdigest(),
        "durable_semantic_config_sha256": hashlib.sha256(_canonical_json(expected_config)).hexdigest(),
        "input_fingerprint_sha256": str(fingerprint.get("hash") or ""),
        "runtime_image_digest": runtime_identity_before["image_digest"],
        "runtime_image_reference_sha256": runtime_identity_before["image_reference_sha256"],
        "runtime_version_sha256": runtime_identity_before["version_sha256"],
        "runtime_incarnation_sha256": runtime_identity_before["incarnation_sha256"],
        "runtime_receipt_config_sha256": str(input_state.get("active_config_hash") or ""),
        "runtime_receipt_fingerprint_sha256": str(input_state.get("fingerprint_hash") or ""),
        "selector_count": len(selectors),
        "selector_inventory_sha256": hashlib.sha256(_canonical_json(selectors)).hexdigest(),
        "selection_target_matches_durable_state": selection_match,
        "selection_revision": (db.get("selection") or {}).get("revision"),
        "native_rule_count": len(rules),
        **checks,
    }


def _mihomo_selection_target_matches(
    db: dict[str, Any], expected_config: dict[str, Any], proxies: dict[str, Any],
) -> bool:
    routing = db.get("routing") if isinstance(db.get("routing"), dict) else {}
    expected_members = {
        str(item.get("name") or "").strip()
        for item in (expected_config.get("proxies") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    mode = str(routing.get("server_mode") or "auto").strip().lower()
    if mode == "auto":
        if str((proxies.get("vpn-global") or {}).get("now") or "").strip() != "vpn-auto":
            return False
        selection = db.get("selection") if isinstance(db.get("selection"), dict) else {}
        selected_server_id = str(selection.get("active_server_id") or "").strip()
        if not selected_server_id:
            # No durable selected member is acceptable only while the native
            # selector remains explicitly fail-closed.
            return str((proxies.get("vpn-auto") or {}).get("now") or "").strip() == "REJECT"
        from fwrouter_api.services.mihomo_config_status import _resolve_active_auto_runtime_target

        expected_target = _resolve_active_auto_runtime_target(selected_server_id, expected_targets=expected_members)
        return expected_target is not None and str((proxies.get("vpn-auto") or {}).get("now") or "").strip() == expected_target
    if mode == "fixed":
        from fwrouter_api.services.mihomo_config_status import _resolve_active_fixed_runtime_target

        fixed_id = str(routing.get("applied_fixed_server_id") or "").strip()
        expected_target = _resolve_active_fixed_runtime_target(fixed_id, expected_targets=expected_members) if fixed_id else None
        return expected_target is not None and str((proxies.get("vpn-global") or {}).get("now") or "").strip() == expected_target
    return False


def _routing_manifest_matches(manifest_routing: Any, routing: Any) -> bool:
    if not isinstance(manifest_routing, dict) or not isinstance(routing, dict):
        return False
    # active_auto_server_id is selector-owned, not part of Core's applied
    # manifest; its current value is independently fenced and checked against
    # Mihomo's native selector.
    keys = (
        "desired_mode", "applied_mode", "selective_default", "server_mode",
        "desired_fixed_server_id", "applied_fixed_server_id",
    )
    return all(str(manifest_routing.get(key) or "") == str(routing.get(key) or "") for key in keys)


def _core_ip_facts() -> dict[str, Any]:
    required: dict[str, bool] = {}
    observed: dict[str, list[Any]] = {}
    for family in ("-4", "-6"):
        rules_result = subprocess.run(
            ["ip", "-N", "-j", family, "rule", "show"],
            check=False, capture_output=True, text=True, timeout=3,
        )
        routes_result = subprocess.run(
            ["ip", "-N", "-j", family, "route", "show", "table", "100"],
            check=False, capture_output=True, text=True, timeout=3,
        )
        if rules_result.returncode != 0 or routes_result.returncode != 0:
            raise ValueError("CORE_IP_READBACK_UNAVAILABLE")
        try:
            rules = json.loads(rules_result.stdout or "[]")
            routes = json.loads(routes_result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("CORE_IP_READBACK_INVALID") from exc
        if not isinstance(rules, list) or not isinstance(routes, list):
            raise ValueError("CORE_IP_READBACK_INVALID")
        rule_rows = [item for item in rules if isinstance(item, dict)]
        route_rows = [item for item in routes if isinstance(item, dict)]
        normalized_rules = [
            (str(item.get("fwmark") or "").lower(), str(item.get("table") or ""),
             int(item.get("priority") or -1))
            for item in rule_rows
        ]
        required[f"{family}_mark_rule"] = ("0x100", "100", 100) in normalized_rules
        required[f"{family}_full_vpn_mark_rule"] = ("0x102", "100", 101) in normalized_rules
        default_routes = {"default"} if family == "-4" else {"default", "::/0"}
        required[f"{family}_local_default_route"] = any(
            str(item.get("type") or "") in {"local", "2"}
            and str(item.get("dst") or "default") in default_routes
            and str(item.get("dev") or "") == "lo"
            for item in route_rows
        )
        observed[family] = {
            # iproute2 returns rules in priority order; preserving this order
            # detects a newly inserted higher-priority policy rule.
            "rules": rule_rows,
            "table_100_routes": sorted(route_rows, key=_canonical_json),
        }
    if not all(required.values()):
        raise ValueError("CORE_IP_POLICY_MISMATCH")
    return {
        "checks": required,
        "semantic_sha256": hashlib.sha256(_canonical_json(observed)).hexdigest(),
        "observed": observed,
    }


def _core_readback(db: dict[str, Any]) -> dict[str, Any]:
    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status
    from fwrouter_api.services.core_intent import read_core_runtime_context

    context_before = read_core_runtime_context()
    manifest = dataplane_global._read_applied_manifest_uncached()
    live_payload = dataplane_status._read_live_dataplane_payload()
    live_mode = dataplane_live._probe_live_global_mode_uncached()
    if context_before is None or not isinstance(manifest, dict) or not isinstance(live_payload, dict):
        raise ValueError("CORE_NATIVE_READBACK_UNAVAILABLE")
    latest = db.get("latest_apply")
    routing = db.get("routing")
    if (not isinstance(latest, dict) or latest.get("status") != "applied"
            or latest.get("core_intent_revision") != db.get("core_revision")
            or not isinstance(routing, dict) or routing.get("apply_state") != "clean"
            or routing.get("desired_mode") != routing.get("applied_mode")
            or manifest.get("plan_id") != latest.get("apply_id")
            or not dataplane_status._live_owned_table_ready(live_payload)
            or live_mode.get("ok") is not True
            or live_mode.get("mode") != routing.get("applied_mode")):
        raise ValueError("CORE_NATIVE_STATE_MISMATCH")
    manifest_routing = manifest.get("routing_global_state")
    if not _routing_manifest_matches(manifest_routing, routing):
        raise ValueError("CORE_APPLIED_MANIFEST_MISMATCH")
    manifest_path = dataplane_global.get_settings().paths.generated_dir / "dataplane" / "applied-manifest.json"
    nft_path = dataplane_global.get_settings().paths.generated_dir / "dataplane" / "applied.nft"
    manifest_sha = _hash_regular_file(manifest_path)
    nft_sha = _hash_regular_file(nft_path)
    receipt = _read_core_native_receipt()
    if (receipt is None
            or receipt.get("apply_id") != latest.get("apply_id")
            or receipt.get("core_intent_revision") != db.get("core_revision")
            or receipt.get("applied_manifest_sha256") != manifest_sha
            or receipt.get("candidate_nft_sha256") != nft_sha):
        raise ValueError("CORE_NATIVE_OWNER_RECEIPT_UNAVAILABLE")
    semantic_sha_before = read_live_nft_semantic_sha256()
    if semantic_sha_before != receipt.get("native_semantics_sha256"):
        raise ValueError("CORE_NATIVE_RULES_MISMATCH")
    ip_facts_before = _core_ip_facts()
    if ip_facts_before["semantic_sha256"] != receipt.get("native_ip_semantics_sha256"):
        raise ValueError("CORE_IP_POLICY_MISMATCH")
    manifest_after = dataplane_global._read_applied_manifest_uncached()
    live_payload_after = dataplane_status._read_live_dataplane_payload()
    live_mode_after = dataplane_live._probe_live_global_mode_uncached()
    ip_facts_after = _core_ip_facts()
    semantic_sha_after = read_live_nft_semantic_sha256()
    if (read_core_runtime_context() != context_before or manifest_after != manifest
            or _hash_regular_file(manifest_path) != manifest_sha or _hash_regular_file(nft_path) != nft_sha
            or semantic_sha_after != semantic_sha_before
            or not isinstance(live_payload_after, dict)
            or live_payload_after.get("required_chains") != live_payload.get("required_chains")
            or live_mode_after.get("mode") != live_mode.get("mode")
            or ip_facts_after != ip_facts_before):
        raise RuntimeError("CORE_RUNTIME_CONTEXT_CHANGED")
    return {
        "apply_owned_by_latest_revision": True,
        "core_intent_revision": db["core_revision"],
        "selection_revision": (db.get("selection") or {}).get("revision"),
        "applied_manifest_stable": True,
        "applied_nft_stable": True,
        "native_rule_semantics_match_owner_receipt": True,
        "native_rule_semantics_sha256": semantic_sha_before,
        "native_ip_semantics_sha256": ip_facts_before["semantic_sha256"],
        "applied_manifest_sha256": manifest_sha,
        "applied_nft_sha256": nft_sha,
        "latest_apply_id_sha256": hashlib.sha256(str(latest.get("apply_id")).encode("utf-8")).hexdigest(),
        "nft_table_ready": True,
        "required_chain_count": sum(bool(value) for value in live_payload.get("required_chains", {}).values()),
        "ip_policy_checks": ip_facts_before["checks"],
        "live_mode": live_mode.get("mode"),
        "ip_rules_stable": True,
        "ip_routes_stable": True,
        "runtime_context_stable": True,
    }


def _read_core_native_receipt() -> dict[str, Any] | None:
    try:
        raw = _read_regular_file(core_native_proof_path(), maximum_bytes=64 * 1024)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if (not isinstance(payload, dict) or payload.get("schema") != 1
            or not isinstance(payload.get("apply_id"), str)
            or type(payload.get("core_intent_revision")) is not int
            or payload.get("normalization") != "nft-table-v1-counter-values-and-dns-timeout-elements-only"
            or any(not isinstance(payload.get(key), str) or not _EXPECTED_SHA256.fullmatch(payload[key])
                   for key in ("candidate_nft_sha256", "applied_manifest_sha256", "native_semantics_sha256",
                               "native_ip_semantics_sha256"))):
        return None
    return payload


def _public_db_facts(db: dict[str, Any], *, checkpoint: dict[str, Any]) -> dict[str, Any]:
    generation = db.get("generation") if isinstance(db.get("generation"), dict) else {}
    selection = db.get("selection") if isinstance(db.get("selection"), dict) else {}
    schema = db.get("schema") if isinstance(db.get("schema"), dict) else {}
    return {
        "schema": {
            "ok": schema.get("ok") is True,
            "expected_version": schema.get("expected_schema_version"),
            "actual_version": schema.get("actual_schema_version"),
        },
        "integrity_check": db.get("integrity"),
        "foreign_key_violations": db.get("foreign_key_violations"),
        "generation": {
            "status": generation.get("status"),
            "candidate_revision": generation.get("candidate_revision"),
            "verified_revision": generation.get("verified_revision"),
            "inventory_count": db.get("inventory_count"),
            "native_proof_present": isinstance(generation.get("native_proof"), dict),
            "native_proof_sha256": (
                hashlib.sha256(_canonical_json(generation["native_proof"])).hexdigest()
                if isinstance(generation.get("native_proof"), dict) else None
            ),
        },
        "selection_revision": selection.get("revision"),
        "core_intent_revision": db.get("core_revision"),
        "latest_apply_status": (db.get("latest_apply") or {}).get("status"),
        "active_job_count": db.get("active_jobs"),
        "checkpoint": {"present": checkpoint.get("present"), "valid": checkpoint.get("valid")},
    }


def _durable_incomplete_reason(db: dict[str, Any], checkpoint: dict[str, Any]) -> str | None:
    schema = db.get("schema") if isinstance(db.get("schema"), dict) else {}
    if (schema.get("ok") is not True or db.get("integrity") != ["ok"]
            or db.get("foreign_key_violations") != 0):
        return "database_integrity_unverified"
    if db.get("active_jobs") != 0:
        return "active_job_present"
    generation = db.get("generation", {})
    if (generation.get("status") != "verified"
            or generation.get("candidate_digest") != generation.get("verified_digest")
            or generation.get("candidate_revision") != generation.get("verified_revision")
            or generation.get("candidate_digest") != db.get("inventory_digest")
            or type(generation.get("candidate_revision")) is not int):
        return "subscription_generation_unverified"
    native_proof = generation.get("native_proof")
    if not _valid_native_generation_proof(native_proof, generation=generation, inventory_digest=db.get("inventory_digest")):
        return "native_generation_receipt_missing_or_invalid"
    modules = {item.get("module_name"): item for item in db.get("modules", []) if isinstance(item, dict)}
    if not all(
        (modules.get(name) or {}).get("desired_state") == "enabled"
        and (modules.get(name) or {}).get("lifecycle_mode") == "managed"
        for name in ("core", "vpn", "xray")
    ):
        return "managed_runtime_scope_unavailable"
    if checkpoint.get("present") is not False:
        return "generation_checkpoint_present_or_invalid"
    return None


def _valid_native_generation_proof(value: Any, *, generation: dict[str, Any], inventory_digest: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema") != 1:
        return False
    if value.get("inventory_digest") != inventory_digest or value.get("inventory_digest") != generation.get("verified_digest"):
        return False
    if (type(value.get("generation_revision")) is not int
            or value.get("generation_revision") != generation.get("verified_revision")):
        return False
    if (not isinstance(value.get("backend_manifest_sha256"), str)
            or not _EXPECTED_SHA256.fullmatch(value["backend_manifest_sha256"])):
        return False
    groups = ("xray", "mihomo", "core")
    required = {
        "xray": ("config_sha256", "runtime_config_sha256", "runtime_incarnation_sha256", "durable_bindings_sha256", "durable_modes_sha256", "runtime_image_reference_sha256", "runtime_version_sha256"),
        "mihomo": ("config_sha256", "semantic_config_sha256", "durable_semantic_config_sha256", "input_fingerprint_sha256", "runtime_incarnation_sha256", "runtime_receipt_config_sha256", "runtime_receipt_fingerprint_sha256", "runtime_image_reference_sha256", "runtime_version_sha256"),
        "core": ("applied_manifest_sha256", "applied_nft_sha256", "native_rule_semantics_sha256",
                 "native_ip_semantics_sha256", "latest_apply_id_sha256", "core_intent_revision", "selection_revision"),
    }
    for group in groups:
        item = value.get(group)
        if not isinstance(item, dict):
            return False
        for name in required[group]:
            field = item.get(name)
            if name.endswith("_sha256"):
                if not isinstance(field, str) or not _EXPECTED_SHA256.fullmatch(field):
                    return False
            elif type(field) is not int or field < 0:
                return False
        if group in {"xray", "mihomo"}:
            if not isinstance(item.get("runtime_image_digest"), str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", item["runtime_image_digest"],
            ):
                return False
    return True


def _make_native_generation_proof(
    backend_sha256: str, *, xray: dict[str, Any], mihomo: dict[str, Any], core: dict[str, Any],
    inventory_digest: str, generation_revision: int,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "inventory_digest": inventory_digest,
        "generation_revision": generation_revision,
        "backend_manifest_sha256": backend_sha256,
        "xray": {key: xray.get(key) for key in (
            "config_sha256", "runtime_config_sha256", "runtime_incarnation_sha256",
            "durable_bindings_sha256", "durable_modes_sha256", "runtime_image_digest",
            "runtime_image_reference_sha256", "runtime_version_sha256",
        )},
        "mihomo": {key: mihomo.get(key) for key in (
            "config_sha256", "semantic_config_sha256", "durable_semantic_config_sha256",
            "input_fingerprint_sha256", "runtime_incarnation_sha256",
            "runtime_receipt_config_sha256", "runtime_receipt_fingerprint_sha256", "runtime_image_digest",
            "runtime_image_reference_sha256", "runtime_version_sha256",
        )},
        "core": {key: core.get(key) for key in (
            "applied_manifest_sha256", "applied_nft_sha256", "native_rule_semantics_sha256", "native_ip_semantics_sha256",
            "latest_apply_id_sha256", "core_intent_revision", "selection_revision",
        )},
    }


def _runtime_readbacks_match(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Require exact stable output equality across the full native-read interval."""
    return first == second


def _with_readonly_projection_sessions(function: Any) -> Any:
    """Make every nested application DB projection read-only during proof."""
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        from fwrouter_api.db.connection import readonly_db_session_scope

        with readonly_db_session_scope():
            return function(*args, **kwargs)

    return wrapped


@_with_readonly_projection_sessions
def capture_native_generation_proof(*, expected_digest: str, expected_revision: int) -> dict[str, Any]:
    """Capture runtime evidence while the existing generation owner holds its writer lock."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
    from fwrouter_api.db.connection import get_db_path

    if not xray_writer_guard_is_held():
        return {"status": "INCOMPLETE", "reason": "generation_owner_lock_missing"}
    try:
        manifest_sha = backend_manifest_sha256(_backend_root())
        before = _db_snapshot(get_db_path())
        generation = before.get("generation") or {}
        if (generation.get("inventory_digest") != expected_digest
                or generation.get("candidate_digest") != expected_digest
                or generation.get("candidate_revision") != expected_revision
                or generation.get("status") not in {"pending", "verified"}):
            return {"status": "STALE", "reason": "generation_owner_fence_changed"}
        if (before.get("schema", {}).get("ok") is not True or before.get("integrity") != ["ok"]
                or before.get("foreign_key_violations") != 0):
            return {"status": "INCOMPLETE", "reason": "database_integrity_unverified"}
        checkpoint = _checkpoint_snapshot()
        if checkpoint.get("present") and checkpoint.get("valid") is not True:
            return {"status": "INCOMPLETE", "reason": "generation_checkpoint_unverifiable"}
        xray = _xray_readback(before)
        mihomo = _mihomo_readback(before)
        core = _core_readback(before)
        # Each reader has local fences; this outer pass catches a component
        # changing after its first read while later components were inspected.
        final_core = _core_readback(before)
        final_mihomo = _mihomo_readback(before)
        final_xray = _xray_readback(before)
        if (not _runtime_readbacks_match(core, final_core)
                or not _runtime_readbacks_match(mihomo, final_mihomo)
                or not _runtime_readbacks_match(xray, final_xray)):
            return {"status": "STALE", "reason": "native_runtime_fence_changed"}
        after = _db_snapshot(get_db_path())
        if (manifest_sha != backend_manifest_sha256(_backend_root())
                or before != after or _checkpoint_snapshot() != checkpoint):
            return {"status": "STALE", "reason": "durable_fence_changed"}
        proof = _make_native_generation_proof(
            manifest_sha, xray=xray, mihomo=mihomo, core=core,
            inventory_digest=expected_digest, generation_revision=expected_revision,
        )
        if not _valid_native_generation_proof(
            proof, generation={"verified_digest": expected_digest, "verified_revision": expected_revision},
            inventory_digest=expected_digest,
        ):
            return {"status": "INCOMPLETE", "reason": "native_generation_receipt_incomplete"}
        return {"status": "PASS", "proof": proof}
    except RuntimeError:
        return {"status": "STALE", "reason": "native_fence_changed"}
    except Exception as exc:
        reason = _safe_failure_reason(exc)
        return {"status": "FAIL" if "MISMATCH" in reason else "INCOMPLETE", "reason": reason}


@_with_readonly_projection_sessions
def collect_native_proof(
    *, scope: str, expected_backend_manifest_sha256: str,
    expected_xray_image_digest: str | None = None, expected_mihomo_image_digest: str | None = None,
) -> dict[str, Any]:
    """Prove stable managed native readbacks; never mutate DB, configs, or runtimes."""
    if scope != "ui":
        return {"status": "INCOMPLETE", "scope": scope, "reason": "unsupported_scope"}
    expected = str(expected_backend_manifest_sha256 or "")
    if not _EXPECTED_SHA256.fullmatch(expected):
        return {"status": "INCOMPLETE", "scope": scope, "reason": "expected_backend_manifest_required"}
    if (not isinstance(expected_xray_image_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_xray_image_digest)
            or not isinstance(expected_mihomo_image_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_mihomo_image_digest)):
        return {"status": "INCOMPLETE", "scope": scope, "reason": "expected_runtime_image_pins_required"}
    try:
        actual = backend_manifest_sha256(_backend_root())
    except (OSError, ValueError):
        return {"status": "INCOMPLETE", "scope": scope, "reason": "backend_manifest_unavailable"}
    result: dict[str, Any] = {
        "proof_schema_version": 1,
        "checks_executed": ["backend_manifest_sha256"],
        "scope": scope,
        "source_identity": {"kind": "backend_manifest_sha256", "sha256": actual, "commit_sha": None},
    }
    if actual != expected:
        result.update(status="STALE", reason="backend_manifest_changed")
        return result
    try:
        from fwrouter_api.adapters.xray_common import xray_writer_read_guard
        from fwrouter_api.db.connection import get_db_path

        with xray_writer_read_guard() as acquired:
            if not acquired:
                result.update(status="INCOMPLETE", reason="xray_writer_barrier_unavailable")
                return result
            before = _db_snapshot(get_db_path())
            before_checkpoint = _checkpoint_snapshot()
            result["checks_executed"].extend(("sqlite_schema_integrity_foreign_keys", "generation_owner_state", "checkpoint_state"))
            precondition_error = _durable_incomplete_reason(before, before_checkpoint)
            if precondition_error is not None:
                result["database"] = _public_db_facts(before, checkpoint=before_checkpoint)
                result.update(status="INCOMPLETE", reason=precondition_error)
                return result
            xray = _xray_readback(before)
            result["checks_executed"].append("xray_identity_bindings_config_runtime")
            mihomo = _mihomo_readback(before)
            result["checks_executed"].append("mihomo_config_rules_selectors_runtime")
            if (xray.get("runtime_image_digest") != expected_xray_image_digest
                    or mihomo.get("runtime_image_digest") != expected_mihomo_image_digest):
                result["database"] = _public_db_facts(before, checkpoint=before_checkpoint)
                result.update(status="STALE", reason="runtime_image_pin_mismatch")
                return result
            core = _core_readback(before)
            result["checks_executed"].append("core_manifest_nft_ip_routing_runtime")
            final_core = _core_readback(before)
            final_mihomo = _mihomo_readback(before)
            final_xray = _xray_readback(before)
            result["checks_executed"].append("final_native_runtime_fence_recheck")
            if (not _runtime_readbacks_match(core, final_core)
                    or not _runtime_readbacks_match(mihomo, final_mihomo)
                    or not _runtime_readbacks_match(xray, final_xray)):
                result.update(status="STALE", reason="native_runtime_fence_changed")
                return result
            after = _db_snapshot(get_db_path())
            after_checkpoint = _checkpoint_snapshot()
        public_facts = _public_db_facts(after, checkpoint=after_checkpoint)
        result["database"] = public_facts
        result["xray"] = xray
        result["mihomo"] = mihomo
        result["core"] = core
        stable = (
            before.get("source_fingerprint") == after.get("source_fingerprint")
            and before.get("schema") == after.get("schema")
            and before.get("integrity") == after.get("integrity")
            and before.get("foreign_key_violations") == after.get("foreign_key_violations")
            and before.get("inventory_digest") == after.get("inventory_digest")
            and before.get("inventory_count") == after.get("inventory_count")
            and before.get("core_revision") == after.get("core_revision")
            and before.get("selection") == after.get("selection")
            and before.get("latest_apply") == after.get("latest_apply")
            and before.get("generation") == after.get("generation")
            and before.get("routing") == after.get("routing")
            and before.get("modules") == after.get("modules")
            and before.get("active_jobs") == after.get("active_jobs")
            and before_checkpoint == after_checkpoint
            and backend_manifest_sha256(_backend_root()) == actual
        )
        db_healthy = (
            after.get("schema", {}).get("ok") is True
            and after.get("integrity") == ["ok"]
            and after.get("foreign_key_violations") == 0
        )
        no_active_jobs = after.get("active_jobs") == 0
        generation_verified = after.get("generation", {}).get("status") == "verified"
        if not stable:
            result.update(status="STALE", reason="durable_fence_changed")
        elif (not db_healthy or not no_active_jobs or not generation_verified
              or _durable_incomplete_reason(after, after_checkpoint) is not None):
            result.update(status="INCOMPLETE", reason="durable_state_not_proof_ready")
        else:
            current_native_proof = _make_native_generation_proof(
                actual, xray=xray, mihomo=mihomo, core=core,
                inventory_digest=str(after.get("inventory_digest") or ""),
                generation_revision=int((after.get("generation") or {}).get("verified_revision") or 0),
            )
            recorded_native_proof = after.get("generation", {}).get("native_proof")
            if recorded_native_proof != current_native_proof:
                result.update(status="STALE", reason="native_generation_receipt_mismatch")
            else:
                result["checks_executed"].append("generation_native_receipt_exact_match")
                result["native_proof_sha256"] = hashlib.sha256(_canonical_json(current_native_proof)).hexdigest()
                result.update(status="PASS", reason=None)
        return result
    except RuntimeError:
        result.update(status="STALE", reason="native_fence_changed")
        return result
    except Exception as exc:
        reason = _safe_failure_reason(exc)
        result.update(status="FAIL" if "MISMATCH" in reason else "INCOMPLETE", reason=reason)
        return result
