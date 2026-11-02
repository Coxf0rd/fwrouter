"""Read-only Xray generation, identity and publication projections.

These views preserve the existing observation windows. The coordinator owns
checkpoint transitions and recovery decisions; this module only captures and
compares current values.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.xray_bindings import collect_xray_runtime_bindings
from fwrouter_api.services.xray_generation_observation import (
    capture_generation_selection as _capture_generation_auto_selection,
    generation_source_fingerprint as _generation_source_fingerprint,
    verify_generation_selection as _verify_generation_selection_readback,
)
from fwrouter_api.services.xray_common import _strip_raw_payload


def capture_derived_rows(
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

def verify_recovery_selection(selection: dict[str, Any] | None) -> dict[str, Any]:
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

def capture_current_projection() -> dict[str, Any]:
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

def current_projection_fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        {"identities": snapshot["identities"], "snapshots": snapshot["snapshots"],
         "fence": snapshot["fence"], "source": _generation_source_fingerprint(),
         "selection": _capture_generation_auto_selection()},
        sort_keys=True, default=str,
    ).encode()).hexdigest()

def bindings_artifact_parity(bindings: list[dict[str, Any]], modes: list[dict[str, Any]]) -> tuple[bool, str]:
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

def verify_public_projection(
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

def supersession_alias_owns(
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


def build_publication_result(
    values: dict[str, Any],
    promoted_profile: dict[str, Any],
    verification: dict[str, Any],
    generation_apply: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the established successful profile publication result shape."""
    desired_nodes = values["desired_nodes"]
    result = {
        "ok": True,
        "status": "success",
        "nodes_count": len(desired_nodes),
        "created_count": len(values["created"]),
        "deleted_count": len(values["deleted"]),
        "recreated_count": len(values["recreated"]),
        "created": values["created"],
        "deleted": values["deleted"],
        "recreated": values["recreated"],
        "client_reconcile": _strip_raw_payload(values["reconcile_details"]),
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "client_uuid": node["client_uuid"],
                "client_email": node["client_email"],
            }
            for node in desired_nodes
        ],
        "materialize": values["materialize_result"],
        "public_profile_promote": promoted_profile,
    }
    if generation_apply is not None:
        result["generation_apply"] = generation_apply
    result["pre_publication_verification"] = verification
    return result


__all__ = [
    "bindings_artifact_parity",
    "build_publication_result",
    "capture_current_projection",
    "capture_derived_rows",
    "current_projection_fingerprint",
    "supersession_alias_owns",
    "verify_public_projection",
    "verify_recovery_selection",
]

# Preserve private lookup names for the extracted bodies. The coordinator
# mirrors only supported compatibility patch points into this namespace.
_current_xray_projection_snapshot = capture_current_projection
_supersession_alias_owns = supersession_alias_owns
