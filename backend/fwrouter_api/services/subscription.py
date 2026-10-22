from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.event_contract import sanitize_string, sanitize_value
from fwrouter_api.services.events import create_event_context, safe_human_label, write_audit_event
from fwrouter_api.adapters.xray_common import xray_writer_guarded, xray_writer_guard_is_held


ALLOWED_SCHEMES = {"http", "https"}
PLACEHOLDER_HOSTS = {
    "subscription.example",
    "example.com",
    "example.net",
    "example.org",
    "localhost",
}
_SUBSCRIPTION_URI = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)
SUBSCRIPTION_NATIVE_GENERATION_KEY = "subscriptions.native_generation.v1"
_SUBSCRIPTION_ADMISSION_ISSUER = object()


@dataclass(frozen=True)
class _SubscriptionInventoryAdmission:
    """Opaque in-process capability for one guarded inventory generation."""

    digest: str
    revision: int
    issuer: object
    proof_only_source_fingerprint: str | None = None
    proof_only_core_runtime_fingerprint: str | None = None
    proof_only_selection_revision: int | None = None
    proof_only_selection_decision_id: str | None = None
    proof_only_selection_snapshot: dict[str, Any] | None = None
    proof_only_routing_snapshot: dict[str, Any] | None = None
    proof_only_xray_incarnation: str | None = None
    proof_only_mihomo_incarnation: str | None = None
    proof_only_xray_config_sha256: str | None = None
    proof_only_mihomo_config_sha256: str | None = None
    proof_only_mihomo_fingerprint_hash: str | None = None
    proof_only_core_manifest_hash: str | None = None
    proof_only_core_apply_id: str | None = None
    proof_only_core_enforcement_level: str | None = None
    proof_only_core_live_mode: str | None = None


class SubscriptionInventoryGenerationBlocked(RuntimeError):
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(str(result.get("reason") or "subscription_inventory_generation_blocked"))


def _subscription_inventory_checkpoint_write_gate() -> dict[str, Any]:
    """Check the Xray generation owner fence without performing native work."""
    from fwrouter_api.services.xray_subscription_service import subscription_inventory_checkpoint_write_gate
    return subscription_inventory_checkpoint_write_gate()


def _subscription_inventory_digest(connection: Any) -> tuple[str, int]:
    """Hash only durable subscription-owned inventory and source memberships."""
    servers = [
        tuple(row)
        for row in connection.execute(
            """SELECT server_id, server_name, provider_name, country_code, region,
                      raw_json, inventory_state
               FROM servers
               WHERE COALESCE(provider_name, '') = 'subscription'
                  OR server_id IN (
                      SELECT server_id FROM subscription_server_memberships WHERE is_active=1
                  )
               ORDER BY server_id"""
        ).fetchall()
    ]
    memberships = [
        tuple(row)
        for row in connection.execute(
            """SELECT source_id, server_id, entry_identity_hash, parser_format, is_active
               FROM subscription_server_memberships
               ORDER BY source_id, server_id"""
        ).fetchall()
    ]
    payload = json.dumps(
        {"servers": servers, "memberships": memberships},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), len(servers)


def _validated_subscription_generation_marker(
    value: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the self-contained generation/proof tuple before it can be reused."""
    try:
        marker = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "generation_marker_invalid"
    if (
        not isinstance(marker, dict)
        or type(marker.get("schema")) is not int
        or marker.get("schema") != 1
    ):
        return None, "generation_marker_invalid"
    status = marker.get("status")
    if not isinstance(status, str) or status not in {"pending", "verified"}:
        return None, "generation_marker_invalid"

    digest_pattern = r"[0-9a-f]{64}"
    candidate_digest = marker.get("candidate_digest")
    if (
        not isinstance(candidate_digest, str)
        or re.fullmatch(digest_pattern, candidate_digest) is None
    ):
        return None, "generation_digest_invalid"
    candidate_revision = marker.get("candidate_revision")
    if type(candidate_revision) is not int or candidate_revision < 1:
        return None, "generation_revision_invalid"

    verified_digest = marker.get("verified_digest")
    verified_revision = marker.get("verified_revision")
    if verified_digest is None and verified_revision is None:
        if status == "verified":
            return None, "verified_generation_proof_mismatch"
    elif (
        not isinstance(verified_digest, str)
        or re.fullmatch(digest_pattern, verified_digest) is None
        or type(verified_revision) is not int
        or verified_revision < 1
    ):
        return None, "verified_generation_proof_mismatch"
    elif status == "verified":
        if verified_digest != candidate_digest or verified_revision != candidate_revision:
            return None, "verified_generation_proof_mismatch"
    elif verified_digest == candidate_digest or verified_revision >= candidate_revision:
        # A pending candidate may retain an older verified generation, but it
        # cannot claim that proof for itself or for a future revision.
        return None, "verified_generation_proof_mismatch"
    return marker, None


def _read_subscription_native_generation(connection: Any) -> dict[str, Any]:
    row = connection.execute(
        "SELECT value_json FROM settings WHERE key=?",
        (SUBSCRIPTION_NATIVE_GENERATION_KEY,),
    ).fetchone()
    current_digest, inventory_count = _subscription_inventory_digest(connection)
    if row is None:
        return {
            "schema": 1,
            "status": "empty" if inventory_count == 0 else "unknown",
            "candidate_digest": None,
            "candidate_revision": None,
            "verified_digest": None,
            "verified_revision": None,
            "reason": None if inventory_count == 0 else "verified_generation_missing",
            "inventory_digest": current_digest,
            "inventory_count": inventory_count,
        }
    marker, invalid_reason = _validated_subscription_generation_marker(row["value_json"])
    if marker is None:
        marker = {}
    status = str(marker.get("status") or "unknown")
    reason = str(marker.get("reason") or "") or invalid_reason
    candidate_digest = marker.get("candidate_digest")
    candidate_revision = marker.get("candidate_revision")
    verified_digest = marker.get("verified_digest")
    verified_revision = marker.get("verified_revision")
    if invalid_reason is not None:
        status, reason = "unknown", invalid_reason
    elif candidate_digest != current_digest:
        status, reason = "unknown", "inventory_digest_mismatch"
    return {
        "schema": 1,
        "status": status,
        "candidate_digest": candidate_digest if isinstance(candidate_digest, str) else None,
        "candidate_revision": candidate_revision if type(candidate_revision) is int else None,
        "verified_digest": verified_digest if isinstance(verified_digest, str) else None,
        "verified_revision": verified_revision if type(verified_revision) is int else None,
        # Internal owner receipt only. Public DTOs below deliberately omit it.
        "native_proof": marker.get("native_proof") if isinstance(marker.get("native_proof"), dict) else None,
        "reason": reason,
        "inventory_digest": current_digest,
        "inventory_count": inventory_count,
    }


def get_subscription_native_generation_state() -> dict[str, Any]:
    """Return redacted generation diagnostics; never expose inventory material."""
    with db_session() as connection:
        state = _read_subscription_native_generation(connection)
    return {key: state[key] for key in (
        "schema", "status", "candidate_digest", "candidate_revision",
        "verified_digest", "verified_revision", "reason", "inventory_count",
    )}


def _write_subscription_inventory_candidate(
    connection: Any, *, force_pending: bool = False,
) -> None:
    current_digest, inventory_count = _subscription_inventory_digest(connection)
    row = connection.execute(
        "SELECT value_json FROM settings WHERE key=?",
        (SUBSCRIPTION_NATIVE_GENERATION_KEY,),
    ).fetchone()
    try:
        raw_prior = json.loads(row["value_json"]) if row else None
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_prior = None
    prior, _invalid_reason = _validated_subscription_generation_marker(raw_prior)
    # A well-typed counter remains a fencing high-water mark even if the proof
    # tuple is malformed. It can advance, but never authorizes reusing proof.
    high_water = None
    if (
        isinstance(raw_prior, dict)
        and type(raw_prior.get("schema")) is int
        and raw_prior.get("schema") == 1
        and type(raw_prior.get("candidate_revision")) is int
        and raw_prior["candidate_revision"] > 0
    ):
        high_water = raw_prior["candidate_revision"]
    if row is not None and high_water is None:
        # With no trustworthy counter, resetting to revision 1 could recreate
        # an old in-process capability. Keep the marker unadmittable instead.
        marker = {
            "schema": 1,
            "status": "unknown",
            "candidate_digest": current_digest,
            "candidate_revision": None,
            "verified_digest": None,
            "verified_revision": None,
            "reason": "generation_revision_high_water_unavailable",
            "inventory_count": inventory_count,
        }
        connection.execute(
            """INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=CURRENT_TIMESTAMP""",
            (SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker, sort_keys=True, separators=(",", ":"))),
        )
        return
    next_revision = (high_water or 0) + 1
    verified_digest = prior.get("verified_digest") if prior is not None else None
    verified_revision = prior.get("verified_revision") if prior is not None else None
    # Returning exactly to a previously proven inventory is safe to admit; all
    # other changed snapshots remain pending until the normal pipeline proves
    # and promotes them.
    verified = bool(
        not force_pending and prior is not None
        and verified_digest and verified_digest == current_digest
    )
    if force_pending and verified_digest == current_digest:
        # A reviewed migration requires a new native publication even if its
        # resulting inventory digest equals an older proven digest. The marker
        # schema cannot represent that as both pending and same-digest last-good.
        verified_digest = None
        verified_revision = None
    marker = {
        "schema": 1,
        "status": "verified" if verified else "pending",
        "candidate_digest": current_digest,
        "candidate_revision": next_revision,
        "verified_digest": current_digest if verified else verified_digest,
        "verified_revision": next_revision if verified else verified_revision,
        "reason": None if verified else "inventory_changed_requires_native_publication",
        "inventory_count": inventory_count,
    }
    connection.execute(
        """INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=CURRENT_TIMESTAMP""",
        (SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker, sort_keys=True, separators=(",", ":"))),
    )


def _subscription_inventory_generation_gate(
    admission: Any = None, *, require_writer_guard: bool = False,
) -> dict[str, Any]:
    with db_session() as connection:
        state = _read_subscription_native_generation(connection)
    if state["status"] == "empty":
        return {"ok": True, "status": "empty", "reason": None}
    if state["status"] == "verified":
        if admission is None:
            return {"ok": True, "status": "verified", "reason": None,
                    "candidate_digest": state["candidate_digest"],
                    "candidate_revision": state["candidate_revision"]}
        # A claimed candidate must still own the exact revision it was issued
        # for, even if another operation has since verified a newer revision.
        if (
            isinstance(admission, _SubscriptionInventoryAdmission)
            and admission.issuer is _SUBSCRIPTION_ADMISSION_ISSUER
            and (not require_writer_guard or xray_writer_guard_is_held())
            and admission.digest == state["candidate_digest"]
            and admission.revision == state["candidate_revision"]
        ):
            return {"ok": True, "status": "verified_admitted", "reason": None,
                    "candidate_digest": state["candidate_digest"],
                    "candidate_revision": state["candidate_revision"]}
        return {"ok": False, "status": "verified", "reason": "inventory_generation_stale",
                "candidate_digest": state["candidate_digest"],
                "candidate_revision": state["candidate_revision"],
                "last_good_retained": True}
    valid_capability = bool(
        isinstance(admission, _SubscriptionInventoryAdmission)
        and admission.issuer is _SUBSCRIPTION_ADMISSION_ISSUER
        and (not require_writer_guard or xray_writer_guard_is_held())
        and state["status"] == "pending"
        and admission.digest == state["candidate_digest"]
        and admission.revision == state["candidate_revision"]
    )
    if valid_capability:
        return {"ok": True, "status": "pending_admitted", "reason": None,
                "candidate_digest": state["candidate_digest"],
                "candidate_revision": state["candidate_revision"]}
    return {"ok": False, "status": state["status"],
            "reason": (
                "inventory_generation_stale"
                if admission is not None and state["status"] == "pending"
                else state["reason"] or "verified_generation_missing"
            ),
            "candidate_digest": state["candidate_digest"],
            "candidate_revision": state["candidate_revision"],
            "last_good_retained": True}


def _claim_subscription_inventory_admission() -> _SubscriptionInventoryAdmission | None:
    if not xray_writer_guard_is_held():
        raise RuntimeError("SUBSCRIPTION_INVENTORY_ADMISSION_REQUIRES_WRITER_GUARD")
    with db_session() as connection:
        state = _read_subscription_native_generation(connection)
    if state["status"] == "verified" or state["status"] == "empty":
        return None
    if state["status"] != "pending" or type(state["candidate_revision"]) is not int:
        raise SubscriptionInventoryGenerationBlocked({
            "ok": False, "status": "pending", "stage": "inventory_generation_admission",
            "error_code": "SUBSCRIPTION_INVENTORY_GENERATION_UNVERIFIED",
            "reason": state["reason"] or "verified_generation_missing",
            "last_good_retained": True,
        })
    return _SubscriptionInventoryAdmission(
        digest=str(state["candidate_digest"]),
        revision=int(state["candidate_revision"]),
        issuer=_SUBSCRIPTION_ADMISSION_ISSUER,
    )


def _promote_subscription_inventory_generation(admission: Any) -> dict[str, Any]:
    if (
        not isinstance(admission, _SubscriptionInventoryAdmission)
        or admission.issuer is not _SUBSCRIPTION_ADMISSION_ISSUER
        or not xray_writer_guard_is_held()
    ):
        return {"ok": False, "reason": "inventory_generation_admission_missing"}
    from fwrouter_api.services.mihomo_reconcile_fingerprint import (
        current_mihomo_input_fingerprint, mihomo_input_unchanged,
        mihomo_udp_policy_receipt_matches,
    )
    proof_routing_snapshot = (
        admission.proof_only_routing_snapshot
        if admission.proof_only_source_fingerprint is not None
        and isinstance(admission.proof_only_routing_snapshot, dict)
        else None
    )
    try:
        fingerprint = current_mihomo_input_fingerprint(proof_routing_snapshot)
    except Exception:
        return {"ok": False, "reason": "native_input_fingerprint_unavailable"}
    if not mihomo_udp_policy_receipt_matches() or not mihomo_input_unchanged(fingerprint):
        return {"ok": False, "reason": "native_receipt_unverified"}
    if admission.proof_only_source_fingerprint is not None:
        from fwrouter_api.services.xray_subscription_service import (
            _legacy_admission_core_runtime_fingerprint,
            _legacy_admission_core_unavailable_reason,
            _legacy_admission_runtime_fences_match,
        )
        if not _legacy_admission_runtime_fences_match(admission):
            return {"ok": False, "reason": "legacy_admission_fence_changed"}
    # The receipt is an additional read-only verification capability, not a new
    # prerequisite for the established inventory promotion path. Legacy source
    # or Core state may lack enough durable proof to qualify; retain the existing
    # owner contract but never preserve an obsolete receipt in that case.
    from fwrouter_api.services.native_proof import capture_native_generation_proof

    native_result = capture_native_generation_proof(
        expected_digest=admission.digest, expected_revision=admission.revision,
    )
    native_proof = (
        native_result.get("proof")
        if native_result.get("status") == "PASS" and isinstance(native_result.get("proof"), dict)
        else None
    )
    with db_session() as connection:
        if admission.proof_only_source_fingerprint is not None:
            # Serialize the final proof+marker CAS with any SQLite writer and
            # compare all durable intent/credential/publication inputs in this
            # same transaction. The filesystem checkpoint remains the durable
            # operation owner across the SQLite commit boundary.
            connection.execute("BEGIN IMMEDIATE")
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
            from fwrouter_api.services.xray_subscription_service import (
                _legacy_admission_core_runtime_fingerprint,
                _legacy_admission_core_unavailable_reason,
                _legacy_admission_source_fingerprint,
            )
            latest_apply = connection.execute(
                "SELECT apply_id, status FROM apply_versions ORDER BY created_at DESC, apply_id DESC LIMIT 1",
            ).fetchone()
            owned_apply = connection.execute(
                "SELECT status FROM apply_versions WHERE apply_id=?",
                (admission.proof_only_core_apply_id,),
            ).fetchone()
            durable_fence = read_selection_fence(connection)
            durable_routing = connection.execute(
                "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, fixed_server_until, "
                "active_auto_server_id, desired_mode, applied_mode, selective_default, apply_state, updated_at "
                "FROM routing_global_state WHERE id=1",
            ).fetchone()
            core_unavailable = _legacy_admission_core_unavailable_reason(connection)
            if core_unavailable is not None:
                return {"ok": False, "reason": core_unavailable}
            if (
                _legacy_admission_source_fingerprint(connection) != admission.proof_only_source_fingerprint
                or durable_fence.get("revision") != admission.proof_only_selection_revision
                or durable_fence.get("decision_id") != admission.proof_only_selection_decision_id
                or durable_routing is None
                or dict(durable_routing) != admission.proof_only_routing_snapshot
                or _legacy_admission_core_runtime_fingerprint(connection)
                != admission.proof_only_core_runtime_fingerprint
                or latest_apply is None or latest_apply["status"] != "applied"
                or str(latest_apply["apply_id"]) != admission.proof_only_core_apply_id
                or owned_apply is None or owned_apply["status"] != "applied"
            ):
                return {"ok": False, "reason": "legacy_admission_fence_changed"}
        current = _read_subscription_native_generation(connection)
        from fwrouter_api.services.core_intent import read_core_intent_revision
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        latest_apply_row = connection.execute(
            "SELECT apply_id, status, core_intent_revision FROM apply_versions "
            "ORDER BY created_at DESC, apply_id DESC LIMIT 1",
        ).fetchone()
        latest_apply = dict(latest_apply_row) if latest_apply_row is not None else None
        proof_core = (native_proof.get("core")
                      if isinstance(native_proof, dict) and isinstance(native_proof.get("core"), dict) else {})
        latest_apply_hash = (
            hashlib.sha256(str(latest_apply.get("apply_id")).encode("utf-8")).hexdigest()
            if isinstance(latest_apply, dict) and latest_apply.get("apply_id") else None
        )
        if native_proof is not None and (proof_core.get("core_intent_revision") != read_core_intent_revision(connection)
                or proof_core.get("selection_revision") != read_selection_fence(connection).get("revision")
                or not isinstance(latest_apply, dict) or latest_apply.get("status") != "applied"
                or latest_apply.get("core_intent_revision") != proof_core.get("core_intent_revision")
                or latest_apply_hash != proof_core.get("latest_apply_id_sha256")):
            native_proof = None
        if (
            current["status"] == "verified"
            and current["candidate_digest"] == admission.digest
            and current["candidate_revision"] == admission.revision
            and current["verified_digest"] == admission.digest
            and current["verified_revision"] == admission.revision
        ):
            # A capability replay cannot replace a previously verified tuple
            # unless the durable intent fences and fresh proof are identical.
            if current.get("native_proof") != native_proof:
                marker = {
                    "schema": 1, "status": "verified",
                    "candidate_digest": admission.digest, "candidate_revision": admission.revision,
                    "verified_digest": admission.digest, "verified_revision": admission.revision,
                    "reason": None, "inventory_count": current["inventory_count"],
                }
                if native_proof is not None:
                    marker["native_proof"] = native_proof
                connection.execute(
                    "UPDATE settings SET value_json=?, updated_at=CURRENT_TIMESTAMP WHERE key=?",
                    (json.dumps(marker, sort_keys=True, separators=(",", ":")), SUBSCRIPTION_NATIVE_GENERATION_KEY),
                )
            return {"ok": True, "digest": admission.digest, "revision": admission.revision,
                    "already_promoted": True}
        if (
            current["status"] != "pending"
            or current["candidate_digest"] != admission.digest
            or current["candidate_revision"] != admission.revision
            or current["inventory_digest"] != admission.digest
        ):
            return {"ok": False, "reason": "inventory_generation_stale"}
        marker = {
            "schema": 1,
            "status": "verified",
            "candidate_digest": admission.digest,
            "candidate_revision": admission.revision,
            "verified_digest": admission.digest,
            "verified_revision": admission.revision,
            "reason": None,
            "inventory_count": current["inventory_count"],
        }
        if native_proof is not None:
            marker["native_proof"] = native_proof
        connection.execute(
            "UPDATE settings SET value_json=?, updated_at=CURRENT_TIMESTAMP WHERE key=?",
            (json.dumps(marker, sort_keys=True, separators=(",", ":")), SUBSCRIPTION_NATIVE_GENERATION_KEY),
        )
    return {"ok": True, "digest": admission.digest, "revision": admission.revision}


def redact_subscription_public_value(value: Any) -> Any:
    """Redact source URLs and credential-like text from subscription DTOs."""
    if isinstance(value, dict):
        public: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key == "display_label":
                public[str(key)] = safe_subscription_source_label_value(item) or ""
            elif normalized_key.endswith("url") or normalized_key in {"url", "source_url", "normalized_url", "subscription_url"}:
                public[str(key)] = "[REDACTED]" if item else item
            else:
                public[str(key)] = redact_subscription_public_value(item)
        return sanitize_value(public)
    if isinstance(value, (list, tuple)):
        return [redact_subscription_public_value(item) for item in value]
    if isinstance(value, str):
        sanitized = sanitize_string(value)
        return _SUBSCRIPTION_URI.sub("[subscription URL redacted]", sanitized)
    return value


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None

    loaded = json.loads(value)
    if isinstance(loaded, dict):
        return loaded

    return {"value": loaded}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _server_to_metadata(server: Any) -> dict[str, Any]:
    return {
        "server_id": server.server_id,
        "server_name": server.server_name,
        "provider_name": server.provider_name,
        "country_code": server.country_code,
        "region": server.region,
        "raw": server.raw,
        "protocol": getattr(server, "protocol", None),
        "host": getattr(server, "host", None),
        "port": getattr(server, "port", None),
        "transport": getattr(server, "transport", None),
        "raw_identity": getattr(server, "raw_identity", None),
        "parser_format": getattr(server, "parser_format", None),
        "source_format": getattr(server, "source_format", None),
        "runtime_name": getattr(server, "runtime_name", None),
    }


def _server_from_metadata(payload: dict[str, Any]) -> Any | None:
    if not isinstance(payload, dict):
        return None

    server_id = str(payload.get("server_id") or "").strip()
    server_name = str(payload.get("server_name") or server_id).strip()
    if not server_id or not server_name:
        return None

    from fwrouter_api.adapters.subscription import SubscriptionServer

    raw = payload.get("raw")
    return SubscriptionServer(
        server_id=server_id,
        server_name=server_name,
        provider_name=payload.get("provider_name"),
        country_code=payload.get("country_code"),
        region=payload.get("region"),
        raw=raw if isinstance(raw, dict) else {},
        protocol=payload.get("protocol"),
        host=payload.get("host"),
        port=payload.get("port"),
        transport=payload.get("transport"),
        raw_identity=payload.get("raw_identity"),
        parser_format=payload.get("parser_format"),
        source_format=payload.get("source_format"),
        runtime_name=payload.get("runtime_name"),
    )


def _subscription_sources(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    subscription = metadata.get("subscriptions") if isinstance(metadata, dict) else None
    items = subscription.get("items") if isinstance(subscription, dict) else None
    if not isinstance(items, list):
        return []

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({**item, "url": url, "enabled": bool(item.get("enabled", True))})
    return sources


def _safe_source_origin(url: str) -> str | None:
    try:
        parsed = urlparse(str(url or "").strip())
        hostname = (parsed.hostname or "").strip().lower()
        if parsed.scheme.lower() not in ALLOWED_SCHEMES or not hostname:
            return None
        # Reject malformed/non-host authority without ever rendering userinfo.
        if any(ch.isspace() for ch in hostname) or any(ch in hostname for ch in "/?#@"):
            return None
        port = parsed.port
        host = f"[{hostname}]" if ":" in hostname else hostname
        return f"{parsed.scheme.lower()}://{host}{':' + str(port) if port else ''}/…"
    except (TypeError, ValueError):
        return None


def safe_subscription_source_label_value(value: Any) -> str | None:
    """Accept only safe human labels and redacted scheme/host origin labels."""
    if not isinstance(value, str):
        return None
    candidate = " ".join(value.split()).strip()
    origin_match = re.fullmatch(r"https?://(?:\[[0-9a-f:]+\]|[a-z0-9.-]+)(?::[0-9]{1,5})?/…(?: \([0-9]+\))?", candidate, re.I)
    if origin_match:
        return candidate
    if safe_human_label(candidate) == candidate:
        return candidate
    return None


def _safe_source_label(source: dict[str, Any]) -> str | None:
    name = source.get("name") or source.get("display_name") or source.get("label")
    if safe_human_label(name) is not None:
        return safe_human_label(name)
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    name = metadata.get("name") or metadata.get("remarks")
    if safe_human_label(name) is not None:
        return safe_human_label(name)
    return _safe_source_origin(str(source.get("url") or ""))


def safe_subscription_source_labels(metadata: dict[str, Any] | None) -> dict[str, str]:
    """Build a non-secret display label for each saved source_ref."""
    labels: dict[str, str] = {}
    sources = _subscription_sources(metadata)
    candidates: list[tuple[str, str]] = []
    totals: dict[str, int] = {}
    for source in sources:
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        label = _safe_source_label(source)
        if not label:
            continue
        candidates.append((_source_id(url), label))
        totals[label] = totals.get(label, 0) + 1
    occurrences: dict[str, int] = {}
    for source_ref, label in candidates:
        occurrences[label] = occurrences.get(label, 0) + 1
        labels[source_ref] = f"{label} ({occurrences[label]})" if totals[label] > 1 else label
    for source in sources:
        url = str(source.get("url") or "").strip()
        if url and _source_id(url) not in labels:
            # The existing localization layer supplies the fallback Source N.
            labels[_source_id(url)] = ""
    return labels


def _subscription_registry_urls_from_metadata(metadata: dict[str, Any] | None) -> list[str]:
    return [
        source["url"]
        for source in _subscription_sources(metadata)
        if bool(source.get("enabled", True))
    ]


def _subscription_url_for_source_ref(source_ref: str, state: dict[str, Any] | None = None) -> str | None:
    """Resolve one stable source reference at the backend action boundary."""
    normalized_ref = str(source_ref or "").strip()
    if re.fullmatch(r"src:[0-9a-f]{64}", normalized_ref) is None:
        return None
    state = state or get_subscription_state()
    metadata = state.get("metadata") if isinstance(state, dict) else None
    for source in _subscription_sources(metadata if isinstance(metadata, dict) else None):
        url = str(source.get("url") or "").strip()
        if url and _source_id(url) == normalized_ref and bool(source.get("enabled", True)):
            return url
    legacy_url = str((state or {}).get("url") or "").strip()
    if legacy_url and _source_id(legacy_url) == normalized_ref:
        return legacy_url
    return None


def _saved_subscription_urls(state: dict[str, Any] | None = None) -> list[str]:
    state = state or get_subscription_state()
    metadata = state.get("metadata") if isinstance(state, dict) else None
    urls: list[Any] = _subscription_registry_urls_from_metadata(metadata)
    if isinstance(state, dict) and state.get("url"):
        urls.append(state.get("url"))
    return normalize_subscription_urls(urls)["urls"]


def _merge_source_metadata(
    *,
    base_metadata: dict[str, Any] | None,
    urls: list[str],
    items: list[dict[str, Any]],
    servers_by_url: dict[str, list[Any]],
    now: str,
    batch: dict[str, Any],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        key: value
        for key, value in (base_metadata or {}).items()
        if key != "subscriptions"
    }

    existing_by_url = {
        source["url"]: source
        for source in _subscription_sources(base_metadata)
    }
    item_by_url = {
        str(item.get("url") or "").strip(): item
        for item in items
        if str(item.get("url") or "").strip()
    }

    source_items: list[dict[str, Any]] = []
    for url in urls:
        previous = existing_by_url.get(url) or {}
        item = item_by_url.get(url) or {}
        ok = bool(item.get("ok"))
        refresh = item.get("refresh") if isinstance(item.get("refresh"), dict) else {}
        previous_servers = previous.get("servers") if isinstance(previous.get("servers"), list) else []
        servers = (
            [
                _server_to_metadata(server)
                for server in servers_by_url.get(url, [])
            ]
            if ok
            else previous_servers
        )

        error = item.get("error") if isinstance(item.get("error"), dict) else None
        source_items.append(
            {
                "url": url,
                "name": _safe_source_label({
                    **previous,
                    "name": previous.get("name") or previous.get("display_name") or previous.get("label"),
                    **({"url": url} if not previous.get("url") else {}),
                    "metadata": refresh.get("metadata") if ok and isinstance(refresh, dict) else previous.get("metadata"),
                }),
                "enabled": True,
                "status": "success" if ok else (previous.get("status") or ("failed" if item else "idle")),
                "last_refresh_at": now if item else previous.get("last_refresh_at"),
                "last_success_at": now if ok else previous.get("last_success_at"),
                "last_error_code": None if ok else ((error or {}).get("code") or previous.get("last_error_code")),
                "last_error_message": None if ok else ((error or {}).get("message") or previous.get("last_error_message")),
                "servers_count": len(servers),
                "servers": servers,
                "metadata": refresh.get("metadata") if ok and isinstance(refresh, dict) else previous.get("metadata"),
                "used_last_good": bool(item and (not ok) and previous_servers),
                "last_refresh_servers_count": (
                    int(item.get("servers_count") or 0)
                    if item
                    else previous.get("last_refresh_servers_count", 0)
                ),
            }
        )

    metadata["batch"] = batch
    metadata["subscriptions"] = {
        "version": 1,
        "updated_at": now,
        "items": source_items,
    }
    return metadata


def _last_good_union_from_sources(sources: list[dict[str, Any]]) -> list[Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        if not bool(source.get("enabled", True)):
            continue
        for server_payload in source.get("servers") or []:
            server = _server_from_metadata(server_payload)
            if server is not None:
                merged.setdefault(server.server_id, server)
    return list(merged.values())


def compact_subscription_metadata(
    metadata: dict[str, Any] | None,
    *,
    redact_urls: bool = False,
) -> dict[str, Any] | None:
    """Return subscription metadata without embedded last-good server snapshots."""

    if not isinstance(metadata, dict):
        return metadata

    public = {
        key: value
        for key, value in metadata.items()
        if key != "subscriptions"
    }
    subscription = metadata.get("subscriptions")
    items = subscription.get("items") if isinstance(subscription, dict) else None
    if isinstance(subscription, dict) and isinstance(items, list):
        public_items: list[dict[str, Any]] = []
        label_by_ref = safe_subscription_source_labels(metadata)
        for item in items:
            if not isinstance(item, dict):
                continue
            item_public = {
                key: value
                for key, value in item.items()
                if key not in {"servers"}
            }
            source_url = str(item_public.get("url") or "").strip()
            if source_url:
                source_ref = _source_id(source_url)
                item_public["source_ref"] = source_ref
                item_public["display_label"] = label_by_ref.get(source_ref, "")
            item_public["name"] = safe_human_label(item_public.get("name"))
            if redact_urls:
                item_public["url_saved"] = bool(item_public.get("url"))
                item_public.pop("url", None)
            item_metadata = item_public.get("metadata")
            if isinstance(item_metadata, dict):
                item_metadata_public = dict(item_metadata)
                item_metadata_public.pop("url", None)
                host_value = item_metadata_public.get("host")
                if isinstance(host_value, str) and any(marker in host_value for marker in ("@", "/", "?", "#")):
                    try:
                        item_metadata_public["host"] = urlparse("//" + host_value).hostname
                    except ValueError:
                        item_metadata_public.pop("host", None)
                item_public["metadata"] = item_metadata_public
            public_items.append(item_public)
        public["subscriptions"] = {
            key: value
            for key, value in subscription.items()
            if key != "items"
        }
        public["subscriptions"]["items"] = public_items
    return public


def validate_subscription_url(url: str | None) -> dict[str, Any]:
    """Validate subscription URL without network access."""

    normalized_url = (url or "").strip()

    if not normalized_url:
        return {
            "valid": False,
            "normalized_url": "",
            "error": {
                "code": "SUBSCRIPTION_URL_EMPTY",
                "message": "Subscription URL is empty.",
            },
        }

    parsed = urlparse(normalized_url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        return {
            "valid": False,
            "normalized_url": normalized_url,
            "error": {
                "code": "SUBSCRIPTION_URL_INVALID_SCHEME",
                "message": "Subscription URL must use http or https.",
            },
        }

    if not parsed.netloc:
        return {
            "valid": False,
            "normalized_url": normalized_url,
            "error": {
                "code": "SUBSCRIPTION_URL_INVALID_HOST",
                "message": "Subscription URL host is missing.",
            },
        }

    hostname = (parsed.hostname or "").strip().lower()
    if hostname in PLACEHOLDER_HOSTS:
        return {
            "valid": False,
            "normalized_url": normalized_url,
            "error": {
                "code": "SUBSCRIPTION_URL_PLACEHOLDER_HOST",
                "message": "Subscription URL host is a placeholder and must be replaced with a real provider URL.",
            },
        }

    return {
        "valid": True,
        "normalized_url": normalized_url,
        "error": None,
    }


def normalize_subscription_urls(urls: list[Any] | None) -> dict[str, Any]:
    """Normalize a user-submitted batch without network access."""

    normalized: list[str] = []
    seen: set[str] = set()
    empty_count = 0
    duplicate_count = 0

    for item in urls or []:
        value = str(item or "").strip()
        if not value:
            empty_count += 1
            continue
        if value in seen:
            duplicate_count += 1
            continue
        seen.add(value)
        normalized.append(value)

    return {
        "urls": normalized,
        "empty_count": empty_count,
        "duplicate_count": duplicate_count,
    }


def get_subscription_state() -> dict[str, Any]:
    """Return current subscription state from SQLite."""

    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref

    exclusive = {"source_ref": get_vpn_auto_exclusive_source_ref()}

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                url,
                status,
                last_refresh_at,
                last_success_at,
                server_inventory_updated_at,
                error_code,
                error_message,
                metadata_json,
                updated_at
            FROM subscription_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        return {
            "url": None,
            "status": "not_configured",
            "last_refresh_at": None,
            "last_success_at": None,
            "server_inventory_updated_at": None,
            "error_code": None,
            "error_message": None,
            "metadata": None,
            "updated_at": None,
            "vpn_auto_exclusive": exclusive,
        }

    return {
        "url": row["url"],
        "status": row["status"],
        "last_refresh_at": row["last_refresh_at"],
        "last_success_at": row["last_success_at"],
        "server_inventory_updated_at": row["server_inventory_updated_at"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "metadata": _json_loads(row["metadata_json"]),
        "updated_at": row["updated_at"],
        "vpn_auto_exclusive": exclusive,
    }


def subscription_state_fingerprint(state: dict[str, Any]) -> str:
    """Fingerprint the source registry snapshot used to build local inventory."""
    material = {"url": state.get("url"), "metadata": state.get("metadata")}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _assert_subscription_snapshot(connection: Any, *, expected_revision: int, expected_state_fingerprint: str) -> None:
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    if read_selection_revision(connection) != expected_revision:
        raise RuntimeError("VPN_AUTO_SELECTION_SNAPSHOT_STALE")
    row = connection.execute("SELECT url, metadata_json FROM subscription_state WHERE id=1").fetchone()
    current_state = {
        "url": row["url"] if row else None,
        "metadata": _json_loads(row["metadata_json"]) if row else None,
    }
    if subscription_state_fingerprint(current_state) != expected_state_fingerprint:
        raise RuntimeError("SUBSCRIPTION_STATE_SNAPSHOT_STALE")


@contextmanager
def _subscription_snapshot_commit(*, expected_revision: int, expected_state_fingerprint: str,
                                  provider_handoffs: list[dict[str, Any]] | None = None):
    """Serialize local inventory + metadata publication against newer source intent.

    Network fetches and provider probes must finish before entering this boundary.
    The shared writer guard is reentrant because inventory upsert also uses it.
    """
    from fwrouter_api.adapters.xray_common import xray_writer_guard

    with xray_writer_guard():
        with db_session() as connection:
            from fwrouter_api.services.provider_managed import validate_provider_inventory_handoffs

            validate_provider_inventory_handoffs(connection, expected_revision, provider_handoffs)
            _assert_subscription_snapshot(
                connection,
                expected_revision=expected_revision,
                expected_state_fingerprint=expected_state_fingerprint,
            )
        yield expected_revision


def subscription_registry_import_plan(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe whether legacy subscription URL state still needs explicit import."""

    state = state or get_subscription_state()
    metadata = state.get("metadata") if isinstance(state, dict) else None
    registry_urls = _subscription_registry_urls_from_metadata(metadata if isinstance(metadata, dict) else None)
    legacy_url = str((state or {}).get("url") or "").strip()
    return {
        "needed": bool(legacy_url and legacy_url not in registry_urls),
        "legacy_url_saved": bool(legacy_url),
        "registry_urls_count": len(registry_urls),
        "action": "post_subscription_with_legacy_url" if legacy_url and legacy_url not in registry_urls else "none",
    }


@xray_writer_guarded
def save_subscription_url(
    url: str,
    *,
    metadata: dict[str, Any] | None = None,
    requested_by: str | None = None,
) -> dict[str, Any]:
    """Save subscription URL as desired/config state.

    This does not refresh provider inventory, does not update Mihomo and does not
    apply dataplane changes. Refresh will be implemented later as a job handler.
    """

    validation = validate_subscription_url(url)
    if not validation["valid"]:
        return {
            "saved": False,
            "validation": validation,
            "state": get_subscription_state(),
        }

    normalized_url = validation["normalized_url"]
    existing_state = get_subscription_state()
    existing_metadata = existing_state.get("metadata") if isinstance(existing_state, dict) else None
    metadata_changed, changed_metadata_fields = _changed_subscription_admin_metadata_fields(
        existing_metadata if isinstance(existing_metadata, dict) else None,
        metadata,
    )
    previous_primary_url = str(existing_state.get("url") or "").strip()
    primary_source_changed = previous_primary_url != normalized_url
    previous_primary_ref = _source_id(previous_primary_url) if previous_primary_url else None
    new_primary_ref = _source_id(normalized_url)
    urls = normalize_subscription_urls([*_saved_subscription_urls(existing_state), normalized_url])["urls"]
    source_added = normalized_url not in _saved_subscription_urls(existing_state)
    now = _utc_timestamp()
    next_metadata = _merge_source_metadata(
        base_metadata=existing_metadata if isinstance(existing_metadata, dict) else metadata,
        urls=urls,
        items=[],
        servers_by_url={},
        now=now,
        batch={
            "submitted_count": len(urls),
            "duplicate_urls": 0,
            "empty_urls": 0,
            "errors": 0,
        },
    )
    if metadata:
        next_metadata.update({key: value for key, value in metadata.items() if key != "subscriptions"})

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_state (
                id,
                url,
                status,
                error_code,
                error_message,
                metadata_json
            )
            VALUES (1, ?, 'idle', NULL, NULL, ?)
            ON CONFLICT(id) DO UPDATE SET
                url = excluded.url,
                status = 'idle',
                error_code = NULL,
                error_message = NULL,
                metadata_json = excluded.metadata_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (normalized_url, _json_dumps(next_metadata)),
        )
        if requested_by and source_added:
            source_ref = _source_id(normalized_url)
            write_audit_event(
                actor=requested_by,
                actor_attribution="caller_supplied",
                source="subscription_admin_api",
                action="subscription_source_added",
                event_code="subscription.source_added",
                legacy_event_type="subscription.source_added",
                entity_type="subscription_source",
                entity_id=source_ref,
                previous_value={"present": False},
                new_value={"present": True, "selected_as_primary": primary_source_changed},
                context=create_event_context(entity_id=source_ref),
                details={
                    "source_ref": source_ref,
                    "entity_label": safe_human_label(metadata.get("name")) if isinstance(metadata, dict) else None,
                    "selected_as_primary": primary_source_changed,
                    "previous_primary_source_ref": previous_primary_ref,
                },
                connection=connection,
            )
        _audit_subscription_configuration_change(
            connection,
            changed_metadata_fields,
            metadata_changed=metadata_changed,
            requested_by=requested_by,
            previous_primary_ref=previous_primary_ref,
            new_primary_ref=new_primary_ref,
            primary_source_changed=primary_source_changed and not source_added,
            source_label=safe_human_label(next_metadata.get("name")),
        )

    return {
        "saved": True,
        "validation": validation,
        "state": get_subscription_state(),
    }


def _source_id(url: str) -> str:
    return "src:" + hashlib.sha256(url.encode("utf-8")).hexdigest()


def _active_xray_server_override_count(connection: Any, server_ids: set[str]) -> int:
    """Count live Xray client assignments that would lose their server target."""
    normalized = sorted({str(server_id).strip() for server_id in server_ids if str(server_id).strip()})
    if not normalized:
        return 0
    placeholders = ", ".join("?" for _ in normalized)
    row = connection.execute(
        f"""SELECT COUNT(*) AS count
            FROM subject_server_overrides o
            JOIN subjects s ON s.subject_id = o.subject_id
            WHERE o.selected_server_id IN ({placeholders})
              AND o.selected_until > CURRENT_TIMESTAMP
              AND s.implementation_kind = 'xray'
              AND s.is_active = 1
              AND s.is_deleted = 0""",
        tuple(normalized),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def delete_subscription_source_intent(
    source_ref: str,
    *,
    requested_by: str = "api.subscription.source.delete",
) -> dict[str, Any]:
    """Delete one exact saved source and deactivate only its memberships.

    Long runtime preflight is collected before the short guarded intent write.
    The mutation retains historical rows and records only the stable digest
    source reference in audit state.
    """
    normalized_ref = str(source_ref or "").strip()
    if not normalized_ref.startswith("src:") or len(normalized_ref) != 68:
        return {"ok": False, "error_code": "SUBSCRIPTION_SOURCE_REF_INVALID", "message": "Subscription source reference is invalid."}
    from fwrouter_api.services.xray_subscription_service import recover_xray_generation_checkpoint_before_inventory_refresh
    recovery = recover_xray_generation_checkpoint_before_inventory_refresh()
    if not recovery.get("ok"):
        return {"ok": False,
                "error_code": str(recovery.get("error_code") or "XRAY_GENERATION_RECOVERY_FAILED"),
                "message": "A verified Xray generation recovery is required before subscription changes."}
    state = get_subscription_state()
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    sources = _subscription_sources(metadata)
    match = next((item for item in sources if _source_id(str(item.get("url") or "")) == normalized_ref), None)
    legacy_primary_url = str(state.get("url") or "").strip()
    if match is None and legacy_primary_url and _source_id(legacy_primary_url) == normalized_ref:
        match = {"url": legacy_primary_url, "enabled": True}
    if match is None:
        return {"ok": False, "error_code": "SUBSCRIPTION_SOURCE_NOT_FOUND", "message": "Saved subscription source was not found."}

    source_url = str(match.get("url") or "")
    source_id = _source_id(source_url)
    from fwrouter_api.services.vpn_auto_selection_state import (
        advance_selection_revision, read_selection_fence, selection_pool_signature,
    )
    with db_session() as connection:
        pool_before = selection_pool_signature(connection)
        expected_fence = read_selection_fence(connection)
        source_server_rows = connection.execute(
            """SELECT DISTINCT m.server_id FROM subscription_server_memberships m
               JOIN servers s ON s.server_id = m.server_id
               WHERE m.source_id = ? AND m.is_active = 1 AND s.inventory_state = 'active'""",
            (source_id,),
        ).fetchall()
        source_server_ids = {str(row["server_id"]) for row in source_server_rows}
        shared_rows = connection.execute(
            """SELECT DISTINCT server_id FROM subscription_server_memberships
               WHERE is_active = 1 AND source_id <> ?""",
            (source_id,),
        ).fetchall()
        shared_ids = {str(row["server_id"]) for row in shared_rows}
        custom_rows = connection.execute("SELECT server_id FROM server_custom_https_proxy").fetchall()
        custom_ids = {str(row["server_id"]) for row in custom_rows}
        orphaned = source_server_ids - shared_ids - custom_ids
        routing_row = connection.execute(
            "SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id, active_auto_server_id FROM routing_global_state WHERE id = 1"
        ).fetchone()

    routing = dict(routing_row) if routing_row is not None else {}
    fixed_logical = str(routing.get("desired_fixed_server_id") or routing.get("applied_fixed_server_id") or "").strip()
    if str(routing.get("server_mode") or "auto").lower() == "fixed" and fixed_logical in orphaned:
        return {"ok": False, "error_code": "SUBSCRIPTION_DELETE_CURRENT_FIXED_SERVER", "message": "This source owns the selected fixed server. Change the fixed server before deleting this source.", "current_server_id": fixed_logical}

    current_auto = str(routing.get("active_auto_server_id") or "").strip()
    if str(routing.get("server_mode") or "auto").lower() == "auto" and current_auto in orphaned:
        from fwrouter_api.services.selector import get_vpn_auto_state
        auto_state = get_vpn_auto_state(read_only=True)
        alternatives = set(str(value) for value in auto_state.get("auto_selectable_candidate_ids") or []) - orphaned
        if not alternatives:
            return {"ok": False, "error_code": "SUBSCRIPTION_DELETE_NO_AUTO_ALTERNATIVE", "message": "The current server belongs only to this source and no eligible alternative is available. Add or enable another VPN-auto server before deleting this source.", "current_server_id": current_auto}

    with db_session() as connection:
        active_xray_overrides = _active_xray_server_override_count(connection, orphaned)
    if active_xray_overrides:
        return {
            "ok": False,
            "error_code": "SUBSCRIPTION_DELETE_ACTIVE_XRAY_OVERRIDES",
            "message": "Clear or reassign active Xray client server overrides that point to servers owned only by this source before deleting it.",
            "active_override_count": active_xray_overrides,
        }

    remaining = [item for item in sources if _source_id(str(item.get("url") or "")) != source_ref]
    if not sources and legacy_primary_url:
        remaining = []
    next_primary = next((str(item.get("url") or "").strip() for item in remaining if item.get("enabled", True)), None)
    next_primary = next_primary or None
    next_metadata = dict(metadata)
    registry = metadata.get("subscriptions") if isinstance(metadata.get("subscriptions"), dict) else {}
    next_registry = dict(registry)
    next_registry["items"] = remaining
    next_metadata["subscriptions"] = next_registry
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    with xray_writer_guard(timeout_seconds=5.0):
        checkpoint_gate = _subscription_inventory_checkpoint_write_gate()
        if not checkpoint_gate.get("ok"):
            return {"ok": False,
                    "error_code": str(checkpoint_gate.get("error_code") or "XRAY_GENERATION_CHECKPOINT_RECOVERY_REQUIRED"),
                    "message": "A verified Xray generation recovery is required before subscription changes."}
        with db_session() as connection:
            # Reserve the SQLite writer before the final dependency check so a
            # concurrent override write cannot land between the check and the
            # source intent mutation.
            connection.execute("BEGIN IMMEDIATE")
            current_fence = read_selection_fence(connection)
            if (current_fence["revision"] != expected_fence["revision"]
                    or current_fence["active_server_id"] != expected_fence["active_server_id"]
                    or current_fence["decision_id"] != expected_fence["decision_id"]
                    or selection_pool_signature(connection) != pool_before):
                return {"ok": False, "error_code": "VPN_AUTO_SELECTION_SNAPSHOT_STALE", "message": "VPN-auto inputs changed during source deletion preflight."}
            from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
            if get_vpn_auto_exclusive_source_ref() == normalized_ref:
                return {"ok": False, "error_code": "SUBSCRIPTION_SOURCE_IS_VPN_AUTO_EXCLUSIVE", "message": "Disable exclusive vpn-auto for this source before deleting it."}
            current_state = get_subscription_state()
            current_metadata = current_state.get("metadata") if isinstance(current_state.get("metadata"), dict) else {}
            if not any(_source_id(str(item.get("url") or "")) == normalized_ref for item in _subscription_sources(current_metadata)):
                if _source_id(str(current_state.get("url") or "")) != normalized_ref:
                    return {"ok": False, "error_code": "VPN_AUTO_SELECTION_SNAPSHOT_STALE", "message": "Subscription sources changed during deletion preflight."}
            active_xray_overrides = _active_xray_server_override_count(connection, orphaned)
            if active_xray_overrides:
                return {
                    "ok": False,
                    "error_code": "SUBSCRIPTION_DELETE_ACTIVE_XRAY_OVERRIDES",
                    "message": "Clear or reassign active Xray client server overrides that point to servers owned only by this source before deleting it.",
                    "active_override_count": active_xray_overrides,
                }
            connection.execute(
                """INSERT INTO subscription_state (id, url, status, error_code, error_message, metadata_json, server_inventory_updated_at)
                   VALUES (1, ?, 'success', NULL, NULL, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(id) DO UPDATE SET url=excluded.url, status='success', error_code=NULL, error_message=NULL,
                     metadata_json=excluded.metadata_json,
                     server_inventory_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP""",
                (next_primary, _json_dumps(next_metadata)),
            )
            # Explicit source deletion is a user intent change, not provider observation.
            connection.execute("UPDATE provider_bindings SET enabled=0, binding_revision=binding_revision+1, last_outcome='source_deleted' WHERE source_ref=?", (source_id,))
            connection.execute(
                "UPDATE subscription_server_memberships SET is_active=0, source_url='deleted:' || source_id, updated_at=CURRENT_TIMESTAMP WHERE source_id=?",
                (source_id,),
            )
            connection.execute(
                """UPDATE servers SET inventory_state='missing', missing_since=COALESCE(missing_since,CURRENT_TIMESTAMP), updated_at=CURRENT_TIMESTAMP
                   WHERE server_id IN (SELECT server_id FROM subscription_server_memberships WHERE source_id=?)
                     AND (COALESCE(provider_name,'')='subscription' OR server_id IN (SELECT logical_server_id FROM provider_bindings WHERE source_ref=?))
                     AND inventory_state='active'
                     AND server_id NOT IN (SELECT server_id FROM subscription_server_memberships WHERE is_active=1)
                     AND server_id NOT IN (SELECT server_id FROM server_custom_https_proxy)""",
                (source_id, source_id),
            )
            # Keep the selected logical intent intact until its replacement is
            # applied and read back. Clearing it here turns inventory loss into an
            # unrequested selection transition before runtime reconciliation.
            write_audit_event(
                actor=requested_by, actor_attribution="caller_supplied", source="subscription_admin_api",
                action="subscription_source_delete_requested", event_code="subscription.source_delete_requested",
                legacy_event_type="subscription.source_delete_requested", entity_type="subscription_source",
                entity_id=source_ref, previous_value={"present": True}, new_value={"present": False},
                context=create_event_context(entity_id=source_ref),
                details={"source_ref": source_ref, "intent": "delete"}, connection=connection,
            )
            resulting_revision = expected_fence["revision"]
            if selection_pool_signature(connection) != pool_before:
                resulting_revision = advance_selection_revision(connection)
                if resulting_revision is None:
                    raise RuntimeError("VPN_AUTO_SELECTION_REVISION_CONFLICT")
    return {
        "ok": True,
        "source_ref": source_ref,
        "deleted": True,
        "previous_primary_source_ref": _source_id(legacy_primary_url) if legacy_primary_url else None,
        "primary_source_ref": _source_id(next_primary) if next_primary else None,
        "orphaned_server_ids": sorted(orphaned),
        "remaining_source_refs": [_source_id(str(item.get("url") or "")) for item in remaining if item.get("url")],
        "current_auto_server_id": current_auto if str(routing.get("server_mode") or "auto").lower() == "auto" else None,
        "selection_revision": resulting_revision,
    }


def _audit_added_subscription_sources(
    connection: Any,
    urls: list[str],
    *,
    requested_by: str | None,
) -> None:
    if not requested_by:
        return
    for url in urls:
        source_ref = _source_id(url)
        write_audit_event(
            actor=requested_by,
            actor_attribution="caller_supplied",
            source="subscription_admin_api",
            action="subscription_source_added",
            event_code="subscription.source_added",
            legacy_event_type="subscription.source_added",
            entity_type="subscription_source",
            entity_id=source_ref,
            previous_value={"present": False},
            new_value={"present": True},
            context=create_event_context(entity_id=source_ref),
            details={
                "source_ref": source_ref,
                "entity_label": None,
                "selected_as_primary": False,
            },
            connection=connection,
        )


_AUDITABLE_SUBSCRIPTION_METADATA_FIELDS = {"name", "description", "enabled"}


_GENERATED_SUBSCRIPTION_METADATA_FIELDS = {"subscriptions", "batch", "stage"}


def _changed_subscription_admin_metadata_fields(
    before: dict[str, Any] | None,
    submitted: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    if not isinstance(submitted, dict):
        return False, []
    current = before if isinstance(before, dict) else {}
    changed_keys = [
        key for key, value in submitted.items()
        if key not in _GENERATED_SUBSCRIPTION_METADATA_FIELDS
        and (key not in current or current.get(key) != value)
    ]
    return bool(changed_keys), sorted(
        key for key in changed_keys
        if key in _AUDITABLE_SUBSCRIPTION_METADATA_FIELDS
    )


def _audit_subscription_configuration_change(
    connection: Any,
    changed_fields: list[str],
    *,
    metadata_changed: bool,
    requested_by: str | None,
    previous_primary_ref: str | None = None,
    new_primary_ref: str | None = None,
    primary_source_changed: bool = False,
    source_label: str | None = None,
) -> None:
    if not requested_by or (not metadata_changed and not primary_source_changed):
        return
    previous_value: dict[str, Any] = {"metadata_changed": False}
    new_value: dict[str, Any] = {
        "metadata_changed": metadata_changed,
        "changed_fields": changed_fields,
    }
    if primary_source_changed:
        previous_value["selected_source_ref"] = previous_primary_ref
        new_value["selected_source_ref"] = new_primary_ref
    write_audit_event(
        actor=requested_by,
        actor_attribution="caller_supplied",
        source="subscription_admin_api",
        action="subscription_configuration_changed",
        event_code="subscription.configuration_changed",
        legacy_event_type="subscription.configuration_changed",
        entity_type="configuration",
        entity_id="subscription:config",
        previous_value=previous_value,
        new_value=new_value,
        context=create_event_context(entity_id="subscription:config"),
        details={
            "metadata_changed": metadata_changed,
            "changed_fields": changed_fields,
            "entity_label": source_label,
        },
        connection=connection,
    )


def _entry_identity_hash(server: Any) -> str:
    raw_identity = str(getattr(server, "raw_identity", "") or "").strip()
    if raw_identity:
        return hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()
    raw = getattr(server, "raw", {}) if isinstance(getattr(server, "raw", {}), dict) else {}
    value = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@xray_writer_guarded
def _upsert_subscription_servers(
    servers: list[Any],
    *,
    servers_by_url: dict[str, list[Any]] | None = None,
    expected_selection_revision: int | None = None,
    expected_subscription_state_fingerprint: str | None = None,
    provider_handoffs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Store parsed subscription servers and source memberships."""

    seen_ids = {server.server_id for server in servers}
    source_map = servers_by_url or {}

    checkpoint_gate = _subscription_inventory_checkpoint_write_gate()
    if not checkpoint_gate.get("ok"):
        raise SubscriptionInventoryGenerationBlocked({
            "ok": False,
            "status": "pending",
            "stage": "generation_recovery",
            "error_code": str(checkpoint_gate.get("error_code") or "XRAY_GENERATION_CHECKPOINT_RECOVERY_REQUIRED"),
            "reason": "verified_generation_recovery_required",
            "last_good_retained": True,
        })

    with db_session() as connection:
        from fwrouter_api.services.vpn_auto_selection_state import (
            advance_selection_revision, selection_pool_signature,
        )
        from fwrouter_api.services.provider_managed import (
            persist_provider_inventory_handoffs,
            validate_provider_inventory_handoffs,
            validate_provider_material_handoff,
        )
        validate_provider_material_handoff(connection)
        if provider_handoffs and expected_selection_revision is None:
            raise RuntimeError("VPN_AUTO_SELECTION_SNAPSHOT_MISSING")
        if expected_selection_revision is not None:
            validate_provider_inventory_handoffs(connection, expected_selection_revision, provider_handoffs)
        if expected_selection_revision is not None:
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
            if read_selection_revision(connection) != expected_selection_revision:
                raise RuntimeError("VPN_AUTO_SELECTION_SNAPSHOT_STALE")
        if expected_subscription_state_fingerprint is not None:
            state_row = connection.execute("SELECT url, metadata_json FROM subscription_state WHERE id=1").fetchone()
            current_state = {
                "url": state_row["url"] if state_row else None,
                "metadata": _json_loads(state_row["metadata_json"]) if state_row else None,
            }
            if subscription_state_fingerprint(current_state) != expected_subscription_state_fingerprint:
                raise RuntimeError("SUBSCRIPTION_STATE_SNAPSHOT_STALE")
        pool_before = selection_pool_signature(connection)
        prior_memberships_by_source: dict[str, set[str]] = {}
        for source_url in source_map:
            source_id = _source_id(source_url)
            prior_memberships_by_source[source_id] = {
                str(row["server_id"]) for row in connection.execute(
                    "SELECT server_id FROM subscription_server_memberships WHERE source_id=? AND is_active=1",
                    (source_id,),
                )
            }
        persist_provider_inventory_handoffs(connection, provider_handoffs)
        preference_transfer_count = 0
        for server in servers:
            prior = connection.execute(
                "SELECT raw_json FROM servers WHERE server_id=?", (server.server_id,)
            ).fetchone()
            if prior:
                try:
                    prior_raw = _json_loads(prior["raw_json"])
                except (TypeError, ValueError):
                    prior_raw = {}
                if isinstance(prior_raw, dict) and prior_raw.get("_fwrouter_identity_migration_required") is True:
                    server.raw["_fwrouter_identity_migration_required"] = True
            connection.execute(
                """
                INSERT INTO servers (
                    server_id,
                    server_name,
                    provider_name,
                    country_code,
                    region,
                    raw_json,
                    inventory_state,
                    missing_since
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', NULL)
                ON CONFLICT(server_id) DO UPDATE SET
                    server_name = excluded.server_name,
                    provider_name = excluded.provider_name,
                    country_code = excluded.country_code,
                    region = excluded.region,
                    raw_json = excluded.raw_json,
                    inventory_state = 'active',
                    last_seen_at = CURRENT_TIMESTAMP,
                    missing_since = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    server.server_id,
                    server.server_name,
                    server.provider_name,
                    server.country_code,
                    server.region,
                    _json_dumps(server.raw),
                ),
            )

            connection.execute(
                "INSERT OR IGNORE INTO server_preferences (server_id) VALUES (?)",
                (server.server_id,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO server_ping_state (server_id) VALUES (?)",
                (server.server_id,),
            )

        from fwrouter_api.services.logical_topology import sync_logical_topology
        sync_logical_topology(connection, servers)

        removed_membership_count = 0
        legacy_membership_deactivated_count = 0
        removed_membership_ids_by_source: dict[str, set[str]] = {}
        if source_map:
            for source_url, source_servers in source_map.items():
                source_id = _source_id(source_url)
                source_seen_ids = {server.server_id for server in source_servers}
                for server in source_servers:
                    connection.execute(
                        """
                        INSERT INTO subscription_server_memberships (
                            source_id,
                            server_id,
                            source_url,
                            entry_identity_hash,
                            parser_format,
                            display_name,
                            is_active
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(source_id, server_id) DO UPDATE SET
                            source_url = excluded.source_url,
                            entry_identity_hash = excluded.entry_identity_hash,
                            parser_format = excluded.parser_format,
                            display_name = excluded.display_name,
                            is_active = 1,
                            last_seen_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            source_id,
                            server.server_id,
                            source_url,
                            _entry_identity_hash(server),
                            getattr(server, "parser_format", None),
                            server.server_name,
                        ),
                    )
                if source_seen_ids:
                    placeholders = ", ".join("?" for _ in source_seen_ids)
                    removed_membership_ids_by_source[source_id] = {
                        str(row["server_id"]) for row in connection.execute(
                            f"SELECT server_id FROM subscription_server_memberships WHERE source_id=? "
                            f"AND is_active=1 AND server_id NOT IN ({placeholders})",
                            (source_id, *tuple(sorted(source_seen_ids))),
                        )
                    }
                    removed_membership_count += connection.execute(
                        f"""
                        UPDATE subscription_server_memberships
                        SET is_active = 0,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE source_id = ?
                          AND server_id NOT IN ({placeholders})
                          AND is_active = 1
                        """,
                        (source_id, *tuple(sorted(source_seen_ids))),
                    ).rowcount
                else:
                    removed_membership_ids_by_source[source_id] = {
                        str(row["server_id"]) for row in connection.execute(
                            "SELECT server_id FROM subscription_server_memberships WHERE source_id=? AND is_active=1",
                            (source_id,),
                        )
                    }
                    removed_membership_count += connection.execute(
                        """
                        UPDATE subscription_server_memberships
                        SET is_active = 0,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE source_id = ?
                          AND is_active = 1
                        """,
                        (source_id,),
                    ).rowcount
                for server in source_servers:
                    preference_transfer_count += _carry_forward_subscription_server_preferences(
                        connection,
                        source_id=source_id,
                        server_id=server.server_id,
                        display_name=server.server_name,
                    )
            legacy_membership_deactivated_count = connection.execute(
                """
                UPDATE subscription_server_memberships
                SET is_active = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_url LIKE 'legacy:%'
                  AND is_active = 1
                """
            ).rowcount
            connection.execute(
                """
                UPDATE servers
                SET
                    inventory_state = 'missing',
                    missing_since = COALESCE(missing_since, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE COALESCE(provider_name, '') = 'subscription'
                  AND inventory_state = 'active'
                  AND server_id NOT IN (
                      SELECT server_id
                      FROM subscription_server_memberships
                      WHERE is_active = 1
                  )
                  AND server_id NOT IN (
                      SELECT server_id FROM server_custom_https_proxy
                  )
                """
            )
        else:
            # Compatibility path for last-good metadata fallback. Keep old union
            # semantics when no concrete source refresh is available.
            if seen_ids:
                placeholders = ", ".join("?" for _ in seen_ids)
                connection.execute(
                    f"""
                    UPDATE servers
                    SET
                        inventory_state = 'missing',
                        missing_since = COALESCE(missing_since, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE COALESCE(provider_name, '') = 'subscription'
                      AND inventory_state = 'active'
                      AND server_id NOT IN ({placeholders})
                      AND server_id NOT IN (
                          SELECT server_id FROM server_custom_https_proxy
                      )
                    """,
                    tuple(sorted(seen_ids)),
                )
            else:
                connection.execute(
                    """
                    UPDATE servers
                    SET
                        inventory_state = 'missing',
                        missing_since = COALESCE(missing_since, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE COALESCE(provider_name, '') = 'subscription'
                      AND inventory_state = 'active'
                      AND server_id NOT IN (
                          SELECT server_id FROM server_custom_https_proxy
                      )
                    """
                )

        if source_map and seen_ids:
            placeholders = ", ".join("?" for _ in seen_ids)
            connection.execute(
                f"""
                UPDATE servers
                SET
                    inventory_state = 'active',
                    missing_since = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE server_id IN ({placeholders})
                """,
                tuple(sorted(seen_ids)),
            )

        # active_auto_server_id is persistent selection intent. Runtime-safe
        # transition is performed by the existing selector after apply.
        stale_active_auto_cleared_count = 0

        if source_map:
            from fwrouter_api.services.subscription_node_identity import inherit_profile_growth
            introduced = {
                _source_id(source_url): {str(server.server_id) for server in source_servers}
                - prior_memberships_by_source.get(_source_id(source_url), set())
                for source_url, source_servers in source_map.items()
            }
            inherit_profile_growth(connection, servers, removed_membership_ids_by_source, introduced)

        active_count = connection.execute(
            "SELECT COUNT(*) FROM servers WHERE inventory_state = 'active'"
        ).fetchone()[0]
        missing_count = connection.execute(
            "SELECT COUNT(*) FROM servers WHERE inventory_state = 'missing'"
        ).fetchone()[0]
        existing_vpn_auto_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM server_preferences
            WHERE COALESCE(vpn_auto, 0) = 1
              AND COALESCE(manually_deleted_at, '') = ''
            """
        ).fetchone()[0]
        vpn_auto_seeded_count = 0
        if existing_vpn_auto_count == 0 and seen_ids:
            placeholders = ", ".join("?" for _ in seen_ids)
            vpn_auto_seeded_count = connection.execute(
                f"""
                UPDATE server_preferences
                SET
                    vpn_auto = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE server_id IN ({placeholders})
                  AND COALESCE(manually_deleted_at, '') = ''
                """,
                tuple(sorted(seen_ids)),
            ).rowcount
        if selection_pool_signature(connection) != pool_before:
            advance_selection_revision(connection)
        from fwrouter_api.services.provider_managed import adopt_provider_inventory_revision
        adopt_provider_inventory_revision(connection)
        # Persist the native-generation admission marker in the same SQLite
        # transaction as the inventory and source-membership mutation.
        _write_subscription_inventory_candidate(connection)

    return {
        "seen_count": len(seen_ids),
        "active_count": active_count,
            "missing_count": missing_count,
            "vpn_auto_seeded_count": vpn_auto_seeded_count,
            "removed_membership_count": removed_membership_count,
            "legacy_membership_deactivated_count": legacy_membership_deactivated_count,
            "stale_active_auto_cleared_count": stale_active_auto_cleared_count,
            "preference_transfer_count": preference_transfer_count,
        }


def _carry_forward_subscription_server_preferences(
    connection: Any,
    *,
    source_id: str,
    server_id: str,
    display_name: str,
) -> int:
    current = connection.execute(
        """
        SELECT vpn_auto, vpn_auto_priority, vpn_auto_priority_origin, global_list,
               remembered_until, manually_deleted_at
        FROM server_preferences
        WHERE server_id = ?
        """,
        (server_id,),
    ).fetchone()
    if current is None:
        return 0
    current_is_default = (
        int(current["vpn_auto"] or 0) == 0
        and int(current["vpn_auto_priority"] or 0) == 0
        and str(current["vpn_auto_priority_origin"] or "legacy") == "legacy"
        and int(current["global_list"] if current["global_list"] is not None else 1) == 1
        and not current["remembered_until"]
        and not current["manually_deleted_at"]
    )
    if not current_is_default:
        return 0

    previous = connection.execute(
        """
        SELECT p.vpn_auto, p.vpn_auto_priority, p.vpn_auto_priority_origin,
               p.global_list, p.remembered_until, p.manually_deleted_at
        FROM subscription_server_memberships AS m
        JOIN server_preferences AS p ON p.server_id = m.server_id
        WHERE m.source_id = ?
          AND m.server_id <> ?
          AND m.is_active = 0
          AND COALESCE(m.display_name, '') = ?
          AND NOT EXISTS (
              SELECT 1
              FROM subscription_server_memberships AS active_m
              WHERE active_m.server_id = m.server_id
                AND active_m.is_active = 1
          )
          AND (
              COALESCE(p.vpn_auto, 0) != 0
              OR COALESCE(p.vpn_auto_priority, 0) != 0
              OR COALESCE(p.vpn_auto_priority_origin, 'legacy') != 'legacy'
              OR COALESCE(p.global_list, 1) != 1
              OR COALESCE(p.remembered_until, '') != ''
              OR COALESCE(p.manually_deleted_at, '') != ''
          )
        ORDER BY m.last_seen_at DESC, p.updated_at DESC, m.updated_at DESC
        LIMIT 1
        """,
        (source_id, server_id, display_name),
    ).fetchone()
    if previous is None:
        return 0

    connection.execute(
        """
        UPDATE server_preferences
        SET
            vpn_auto = ?,
            vpn_auto_priority = ?,
            vpn_auto_priority_origin = ?,
            global_list = ?,
            remembered_until = ?,
            manually_deleted_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE server_id = ?
        """,
        (
            int(previous["vpn_auto"] or 0),
            int(previous["vpn_auto_priority"] or 0),
            str(previous["vpn_auto_priority_origin"] or "legacy"),
            int(previous["global_list"] if previous["global_list"] is not None else 1),
            previous["remembered_until"],
            previous["manually_deleted_at"],
            server_id,
        ),
    )
    return 1


def _existing_server_ids(server_ids: set[str]) -> set[str]:
    if not server_ids:
        return set()

    placeholders = ", ".join("?" for _ in server_ids)
    with db_session() as connection:
        rows = connection.execute(
            f"SELECT server_id FROM servers WHERE server_id IN ({placeholders})",
            tuple(sorted(server_ids)),
        ).fetchall()

    return {str(row["server_id"]) for row in rows}


def refresh_subscription_inventory_batch(
    urls: list[Any],
    *,
    metadata: dict[str, Any] | None = None,
    requested_by: str | None = None,
    only_source_ref: str | None = None,
) -> dict[str, Any]:
    """Download several subscriptions and sync their union into SQLite once.

    This does not generate Mihomo config, does not restart Mihomo and does not
    apply dataplane changes.
    """

    from fwrouter_api.adapters.subscription import DEFAULT_SUBSCRIPTION_ADAPTER
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    with db_session() as connection:
        expected_selection_revision = read_selection_revision(connection)

    state_before = get_subscription_state()
    expected_subscription_fingerprint = subscription_state_fingerprint(state_before)
    existing_metadata = state_before.get("metadata") if isinstance(state_before, dict) else None
    metadata_changed, changed_metadata_fields = _changed_subscription_admin_metadata_fields(
        existing_metadata if isinstance(existing_metadata, dict) else None,
        metadata,
    )
    normalized = normalize_subscription_urls(urls)
    submitted_urls: list[str] = normalized["urls"]
    existing_urls_before = set(_saved_subscription_urls(state_before))
    added_source_urls = [url for url in submitted_urls if url not in existing_urls_before]
    if not submitted_urls:
        return {
            "ok": False,
            "stage": "validate",
            "validation": {
                "valid": False,
                "normalized_url": "",
                "error": {
                    "code": "SUBSCRIPTION_URL_EMPTY",
                    "message": "Subscription URL is empty.",
                },
            },
            "state": get_subscription_state(),
            "inventory": None,
            "batch": {
                "submitted_count": 0,
                "requested_count": len(urls or []),
                "added_subscriptions": 0,
                "imported_servers": 0,
                "already_existing": normalized["duplicate_count"],
                "duplicate_urls": normalized["duplicate_count"],
                "empty_urls": normalized["empty_count"],
                "errors": 1,
                "items": [],
            },
            "error": {
                "code": "SUBSCRIPTION_URL_EMPTY",
                "message": "Subscription URL is empty.",
            },
        }

    batch_urls = normalize_subscription_urls([*_saved_subscription_urls(state_before), *submitted_urls])["urls"]
    items: list[dict[str, Any]] = []
    validation_items: dict[str, dict[str, Any]] = {}
    servers_by_url: dict[str, list[Any]] = {}
    provider_handoffs: list[dict[str, Any]] = []
    merged_servers_by_id: dict[str, Any] = {}
    last_successful_url: str | None = None
    errors = 0

    fetch_urls = batch_urls
    if only_source_ref is not None:
        source_url = _subscription_url_for_source_ref(only_source_ref, state_before)
        if source_url is None:
            return {
                "ok": False,
                "stage": "validate",
                "state": state_before,
                "batch": {"submitted_count": len(batch_urls), "requested_count": 1, "errors": 1, "items": [], "targeted_source_ref": only_source_ref, "targeted": True},
                "error": {"code": "SUBSCRIPTION_SOURCE_NOT_FOUND", "message": "Saved subscription source was not found."},
            }
        fetch_urls = [source_url]

    validated_urls: list[str] = []
    for refresh_url in fetch_urls:
        validation = validate_subscription_url(refresh_url)
        if not validation["valid"]:
            errors += 1
            validation_items[refresh_url] = {
                "url": refresh_url,
                "ok": False,
                "stage": "validate",
                "servers_count": 0,
                "error": validation["error"],
            }
            continue
        validated_urls.append(validation["normalized_url"])

    fetch_started_at = perf_counter()

    def fetch_one(refresh_url: str) -> tuple[str, Any]:
        from fwrouter_api.services.provider_managed import fetch_provider_subscription
        managed = fetch_provider_subscription(_source_id(refresh_url))
        return refresh_url, managed if managed is not None else DEFAULT_SUBSCRIPTION_ADAPTER.refresh(refresh_url)

    if len(validated_urls) > 1:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="subscription-fetch") as executor:
            fetched = dict(executor.map(fetch_one, validated_urls))
    else:
        fetched = {}
        if validated_urls:
            url_key, result = fetch_one(validated_urls[0])
            fetched[url_key] = result

    fetched_items: dict[str, dict[str, Any]] = {}
    for refresh_url in validated_urls:
        refresh_result = fetched[refresh_url]
        if not refresh_result.ok:
            errors += 1
            fetched_items[refresh_url] = {
                "url": refresh_url,
                "ok": False,
                "stage": "download_parse",
                "servers_count": 0,
                "error": {
                    "code": refresh_result.error_code,
                    "message": refresh_result.error_message,
                },
                "refresh": refresh_result.to_dict(),
            }
            continue

        servers_by_url[refresh_url] = list(refresh_result.servers)
        provider_handoff = getattr(refresh_result, "_fwrouter_provider_handoff", None)
        if isinstance(provider_handoff, dict):
            provider_handoffs.append(provider_handoff)
        for server in refresh_result.servers:
            merged_servers_by_id.setdefault(server.server_id, server)
        last_successful_url = refresh_url
        fetched_items[refresh_url] = {
            "url": refresh_url,
            "ok": True,
            "stage": "download_parse",
            "servers_count": len(refresh_result.servers),
            "error": None,
            "refresh": refresh_result.to_dict(),
        }

    items = []
    for url in batch_urls:
        item = validation_items.get(url) or fetched_items.get(url)
        if only_source_ref is not None and item is None:
            # Untargeted sources remain present in the normalized inventory and
            # membership union without being fetched or timestamped.
            continue
        if item is None:
            raise RuntimeError(f"Subscription refresh result missing for validated URL: {url}")
        items.append(item)

    now = _utc_timestamp()
    fetch_total_ms = round((perf_counter() - fetch_started_at) * 1000, 2)
    batch_summary = {
        "submitted_count": len(batch_urls),
        "requested_count": len(urls or []),
        "new_urls_count": len(submitted_urls),
        "duplicate_urls": normalized["duplicate_count"],
        "empty_urls": normalized["empty_count"],
        "errors": errors,
        "provider_fetch_total_ms": fetch_total_ms,
    }
    next_metadata = _merge_source_metadata(
        base_metadata=existing_metadata if isinstance(existing_metadata, dict) else metadata,
        urls=batch_urls,
        items=items,
        servers_by_url=servers_by_url,
        now=now,
        batch=batch_summary,
    )
    if metadata:
        next_metadata.update({key: value for key, value in metadata.items() if key != "subscriptions"})
        next_metadata["batch"] = batch_summary

    merged_servers = list(merged_servers_by_id.values())
    targeted_servers = list(merged_servers)
    if only_source_ref is not None:
        # A targeted fetch replaces only its own normalized inventory. Retain
        # the peers' last-good nodes in the effective union without refreshing
        # their memberships or pretending they were fetched.
        targeted_url = _subscription_url_for_source_ref(only_source_ref, state_before)
        for source in _subscription_sources(next_metadata):
            source_url = str(source.get("url") or "").strip()
            if not source_url or source_url == targeted_url:
                continue
            for server in [
                _server_from_metadata(payload)
                for payload in source.get("servers") or []
                if isinstance(payload, dict)
            ]:
                if server is not None:
                    merged_servers_by_id.setdefault(server.server_id, server)
        merged_servers = list(merged_servers_by_id.values())
    if errors:
        for server in _last_good_union_from_sources(_subscription_sources(next_metadata)):
            merged_servers_by_id.setdefault(server.server_id, server)
        merged_servers = list(merged_servers_by_id.values())

    existing_server_ids = _existing_server_ids(set(merged_servers_by_id.keys()))
    inventory_servers = targeted_servers if only_source_ref is not None else merged_servers
    inventory = None
    imported_servers = max(0, len(merged_servers_by_id) - len(existing_server_ids))
    added_subscriptions = sum(1 for item in items if item.get("ok"))
    already_existing = (
        normalized["duplicate_count"]
        + len(existing_server_ids)
        + sum(max(0, int(item.get("servers_count") or 0)) for item in items if item.get("ok"))
        - len(merged_servers_by_id)
    )

    if last_successful_url:
        with _subscription_snapshot_commit(
            expected_revision=expected_selection_revision,
            expected_state_fingerprint=expected_subscription_fingerprint,
            provider_handoffs=provider_handoffs,
        ):
            if merged_servers or servers_by_url:
                inventory = _upsert_subscription_servers(
                    inventory_servers,
                    servers_by_url=servers_by_url,
                    expected_selection_revision=expected_selection_revision,
                    expected_subscription_state_fingerprint=expected_subscription_fingerprint,
                    provider_handoffs=provider_handoffs,
                )
            with db_session() as connection:
                connection.execute(
                """
                INSERT INTO subscription_state (
                    id,
                    url,
                    status,
                    error_code,
                    error_message,
                    metadata_json,
                    last_refresh_at,
                    last_success_at,
                    server_inventory_updated_at
                )
                VALUES (
                    1,
                    ?,
                    'success',
                    NULL,
                    NULL,
                    ?,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(id) DO UPDATE SET
                    url = excluded.url,
                    status = 'success',
                    error_code = NULL,
                    error_message = NULL,
                    metadata_json = excluded.metadata_json,
                    last_refresh_at = CURRENT_TIMESTAMP,
                    last_success_at = CURRENT_TIMESTAMP,
                    server_inventory_updated_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                    (last_successful_url, _json_dumps(next_metadata)),
                )
                _audit_added_subscription_sources(
                    connection,
                    added_source_urls,
                    requested_by=requested_by,
                )
                _audit_subscription_configuration_change(
                    connection,
                    changed_metadata_fields,
                    metadata_changed=metadata_changed,
                    requested_by=requested_by,
                )
    else:
        first_error = next((item.get("error") for item in items if item.get("error")), None)
        with _subscription_snapshot_commit(
            expected_revision=expected_selection_revision,
            expected_state_fingerprint=expected_subscription_fingerprint,
            provider_handoffs=provider_handoffs,
        ):
            last_good_servers = _last_good_union_from_sources(_subscription_sources(next_metadata))
            inventory = (_upsert_subscription_servers(
                last_good_servers,
                expected_selection_revision=expected_selection_revision,
                expected_subscription_state_fingerprint=expected_subscription_fingerprint,
                provider_handoffs=provider_handoffs,
            ) if last_good_servers else None)
            with db_session() as connection:
                connection.execute(
                """
                INSERT INTO subscription_state (
                    id,
                    status,
                    error_code,
                    error_message,
                    metadata_json
                )
                VALUES (1, 'failed', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = 'failed',
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    metadata_json = excluded.metadata_json,
                    last_refresh_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                    (
                        (first_error or {}).get("code") or "SUBSCRIPTION_BATCH_FAILED",
                        (first_error or {}).get("message") or "Subscription batch failed.",
                        _json_dumps(next_metadata),
                    ),
                )
                _audit_added_subscription_sources(
                    connection,
                    added_source_urls,
                    requested_by=requested_by,
                )
                _audit_subscription_configuration_change(
                    connection,
                    changed_metadata_fields,
                    metadata_changed=metadata_changed,
                    requested_by=requested_by,
                )

    return {
        "ok": bool(last_successful_url),
        "stage": "inventory_synced" if last_successful_url else "download_parse",
        "validation": None,
        "state": get_subscription_state(),
        "inventory": inventory,
        "batch": {
            "submitted_count": len(batch_urls),
            "requested_count": len(urls or []),
            "added_subscriptions": added_subscriptions,
            "imported_servers": imported_servers,
            "already_existing": already_existing,
            "duplicate_urls": normalized["duplicate_count"],
            "empty_urls": normalized["empty_count"],
            "errors": errors,
            "items": items,
            "provider_fetch_total_ms": fetch_total_ms,
            "targeted_source_ref": only_source_ref,
            "targeted": only_source_ref is not None,
        },
        "error": None if last_successful_url else {
            "code": "SUBSCRIPTION_BATCH_FAILED",
            "message": "All subscription URLs failed.",
        },
    }


def refresh_subscription_inventory(
    url: str | None = None,
) -> dict[str, Any]:
    """Download subscription and sync parsed servers into SQLite.

    This does not generate Mihomo config, does not restart Mihomo and does not
    apply dataplane changes.
    """

    from fwrouter_api.adapters.subscription import DEFAULT_SUBSCRIPTION_ADAPTER
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    with db_session() as connection:
        expected_selection_revision = read_selection_revision(connection)

    state = get_subscription_state()
    expected_subscription_fingerprint = subscription_state_fingerprint(state)
    requested_urls = normalize_subscription_urls([*_saved_subscription_urls(state), url] if url else _saved_subscription_urls(state))["urls"]
    if len(requested_urls) > 1:
        return refresh_subscription_inventory_batch([url] if url else requested_urls)

    refresh_url = (url or state.get("url") or "").strip()
    existing_metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else None

    validation = validate_subscription_url(refresh_url)
    if not validation["valid"]:
        next_metadata = _merge_source_metadata(
            base_metadata=existing_metadata,
            urls=[refresh_url] if refresh_url else _saved_subscription_urls(state),
            items=[
                {
                    "url": refresh_url,
                    "ok": False,
                    "stage": "validate",
                    "servers_count": 0,
                    "error": validation["error"],
                }
            ] if refresh_url else [],
            servers_by_url={},
            now=_utc_timestamp(),
            batch={
                "submitted_count": 1 if refresh_url else 0,
                "requested_count": 1 if refresh_url else 0,
                "new_urls_count": 0,
                "duplicate_urls": 0,
                "empty_urls": 0 if refresh_url else 1,
                "errors": 1,
            },
        )
        next_metadata["stage"] = "validate"
        with _subscription_snapshot_commit(
            expected_revision=expected_selection_revision,
            expected_state_fingerprint=expected_subscription_fingerprint,
        ):
            with db_session() as connection:
                connection.execute(
                """
                INSERT INTO subscription_state (
                    id,
                    status,
                    error_code,
                    error_message,
                    metadata_json
                )
                VALUES (1, 'failed', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = 'failed',
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    metadata_json = excluded.metadata_json,
                    last_refresh_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                    (
                        validation["error"]["code"],
                        validation["error"]["message"],
                        _json_dumps(next_metadata),
                    ),
                )

        return {
            "ok": False,
            "stage": "validate",
            "validation": validation,
            "state": get_subscription_state(),
            "inventory": None,
            "error": validation["error"],
            "diagnostics": {
                "kind": "saved_subscription_url_invalid",
                "url_saved": bool(refresh_url),
                "saved_url_invalid": True,
                "saved_url_reason": validation["error"]["code"],
            },
        }

    refresh_result = DEFAULT_SUBSCRIPTION_ADAPTER.refresh(validation["normalized_url"])
    now = _utc_timestamp()

    if not refresh_result.ok:
        item = {
            "url": validation["normalized_url"],
            "ok": False,
            "stage": "download_parse",
            "servers_count": 0,
            "error": {
                "code": refresh_result.error_code,
                "message": refresh_result.error_message,
            },
            "refresh": refresh_result.to_dict(),
        }
        next_metadata = _merge_source_metadata(
            base_metadata=existing_metadata,
            urls=[validation["normalized_url"]],
            items=[item],
            servers_by_url={},
            now=now,
            batch={
                "submitted_count": 1,
                "requested_count": 1,
                "new_urls_count": 0,
                "duplicate_urls": 0,
                "empty_urls": 0,
                "errors": 1,
            },
        )
        with _subscription_snapshot_commit(
            expected_revision=expected_selection_revision,
            expected_state_fingerprint=expected_subscription_fingerprint,
        ):
            with db_session() as connection:
                connection.execute(
                """
                INSERT INTO subscription_state (
                    id,
                    url,
                    status,
                    error_code,
                    error_message,
                    metadata_json
                )
                VALUES (1, ?, 'failed', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url = excluded.url,
                    status = 'failed',
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    metadata_json = excluded.metadata_json,
                    last_refresh_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                    (
                        validation["normalized_url"],
                        refresh_result.error_code,
                        refresh_result.error_message,
                        _json_dumps(next_metadata),
                    ),
                )

        return {
            "ok": False,
            "stage": "download_parse",
            "validation": validation,
            "state": get_subscription_state(),
            "inventory": None,
            "error": {
                "code": refresh_result.error_code,
                "message": refresh_result.error_message,
            },
            "refresh": refresh_result.to_dict(),
            "diagnostics": {
                "kind": "subscription_provider_refresh_failed",
                "url_saved": True,
                "saved_url_invalid": False,
                "saved_url_reason": None,
            },
        }

    item = {
        "url": validation["normalized_url"],
        "ok": True,
        "stage": "download_parse",
        "servers_count": len(refresh_result.servers),
        "error": None,
        "refresh": refresh_result.to_dict(),
    }
    next_metadata = _merge_source_metadata(
        base_metadata=existing_metadata,
        urls=[validation["normalized_url"]],
        items=[item],
        servers_by_url={validation["normalized_url"]: list(refresh_result.servers)},
        now=now,
        batch={
            "submitted_count": 1,
            "requested_count": 1,
            "new_urls_count": 0,
            "duplicate_urls": 0,
            "empty_urls": 0,
            "errors": 0,
        },
    )

    provider_handoff = getattr(refresh_result, "_fwrouter_provider_handoff", None)
    provider_handoffs = [provider_handoff] if isinstance(provider_handoff, dict) else []
    with _subscription_snapshot_commit(
        expected_revision=expected_selection_revision,
        expected_state_fingerprint=expected_subscription_fingerprint,
        provider_handoffs=provider_handoffs,
    ):
        inventory = _upsert_subscription_servers(
            refresh_result.servers,
            servers_by_url={validation["normalized_url"]: list(refresh_result.servers)},
            expected_selection_revision=expected_selection_revision,
            expected_subscription_state_fingerprint=expected_subscription_fingerprint,
            provider_handoffs=provider_handoffs,
        )
        with db_session() as connection:
            connection.execute(
            """
            INSERT INTO subscription_state (
                id,
                url,
                status,
                error_code,
                error_message,
                metadata_json,
                last_refresh_at,
                last_success_at,
                server_inventory_updated_at
            )
            VALUES (
                1,
                ?,
                'success',
                NULL,
                NULL,
                ?,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(id) DO UPDATE SET
                url = excluded.url,
                status = 'success',
                error_code = NULL,
                error_message = NULL,
                metadata_json = excluded.metadata_json,
                last_refresh_at = CURRENT_TIMESTAMP,
                last_success_at = CURRENT_TIMESTAMP,
                server_inventory_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
                (
                    validation["normalized_url"],
                    _json_dumps(next_metadata),
                ),
            )

    return {
        "ok": True,
        "stage": "inventory_synced",
        "validation": validation,
        "state": get_subscription_state(),
        "inventory": inventory,
        "refresh": refresh_result.to_dict(),
    }


def refresh_subscription(source_ref: str) -> dict[str, Any]:
    """Fetch one saved source and reconcile it with retained peer inventories."""
    state = get_subscription_state()
    source_url = _subscription_url_for_source_ref(source_ref, state)
    if source_url is None:
        return {
            "ok": False,
            "stage": "validate",
            "state": state,
            "batch": {"submitted_count": 0, "requested_count": 1, "errors": 1, "items": []},
            "error": {"code": "SUBSCRIPTION_SOURCE_NOT_FOUND", "message": "Saved subscription source was not found."},
        }
    return refresh_subscription_inventory_batch(
        [source_url],
        only_source_ref=source_ref,
    )


def refresh_all_subscriptions() -> dict[str, Any]:
    """Fetch every saved source once, then sync their normalized inventory union."""
    state = get_subscription_state()
    urls = _saved_subscription_urls(state)
    return refresh_subscription_inventory_batch(urls)
