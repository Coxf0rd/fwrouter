from __future__ import annotations

import json
import hashlib
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.auto_eligibility import provider_managed_legacy_sql, provider_internal_member_sql
from fwrouter_api.services.subject_taxonomy import explicit_external_client_allows_virtual_vpn_auto as explicit_external_client_allows_virtual_vpn_auto


VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
MANUAL_SERVER_TTL_HOURS = 24
GLOBAL_FIXED_SERVER_TTL_HOURS = 24


from fwrouter_api.services.server_state import ensure_routing_global_state, get_routing_global_state
from fwrouter_api.services.events import safe_human_label
from fwrouter_api.adapters.xray_common import (
    xray_writer_guard,
    xray_writer_guard_is_held,
    xray_writer_guarded,
)


def _get_active_server_row(server_id: str) -> Any | None:
    with db_session() as connection:
        return connection.execute(
            f"""
            SELECT
                s.server_id,
                ({provider_managed_legacy_sql()}) AS provider_managed_legacy,
                ({provider_internal_member_sql()}) AS provider_internal_member,
                s.server_name,
                s.raw_json,
                s.inventory_state,
                COALESCE(p.vpn_auto, 0) AS vpn_auto,
                COALESCE(p.vpn_auto_priority, 0) AS vpn_auto_priority,
                COALESCE(p.global_list, 1) AS global_list,
                COALESCE(p.manually_deleted_at, '') AS manually_deleted_at
            FROM servers s
            LEFT JOIN server_preferences p ON p.server_id = s.server_id
            WHERE s.server_id = ?
              AND s.inventory_state = 'active'
            """,
            (server_id,),
        ).fetchone()


def _validate_global_fixed_server(server_id: str) -> dict[str, Any]:
    row = _get_active_server_row(server_id)
    if row is None:
        return {
            "ok": False,
            "error_code": "SERVER_NOT_FOUND_OR_INACTIVE",
            "error_message": f"Server is not active in full inventory: {server_id}",
            "server": None,
        }

    if row["provider_managed_legacy"] or row["provider_internal_member"]:
        return {"ok": False, "error_code": "SERVER_PROVIDER_MANAGED" if row["provider_managed_legacy"] else "SERVER_INTERNAL_MEMBER",
                "error_message": "Server is not an independent selectable target.", "server": dict(row)}
    if row["manually_deleted_at"]:
        return {
            "ok": False,
            "error_code": "SERVER_MANUALLY_DELETED",
            "error_message": f"Server is manually deleted: {server_id}",
            "server": dict(row),
        }

    return {
        "ok": True,
        "error_code": None,
        "error_message": None,
        "server": dict(row),
    }


def _mihomo_target_for_server(server: dict[str, Any] | None, fallback_server_id: str) -> str:
    if not isinstance(server, dict):
        return str(fallback_server_id)
    raw = server.get("raw_json")
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            runtime_name = payload.get("_fwrouter_runtime_name") or payload.get("name")
            if isinstance(runtime_name, str) and runtime_name.strip():
                return runtime_name.strip()
    elif isinstance(raw, dict):
        runtime_name = raw.get("_fwrouter_runtime_name") or raw.get("name")
        if isinstance(runtime_name, str) and runtime_name.strip():
            return runtime_name.strip()
    return str(server.get("server_name") or fallback_server_id)


def _validate_user_selectable_server(server_id: str) -> dict[str, Any]:
    validation = _validate_global_fixed_server(server_id)
    if not validation["ok"]:
        return validation

    server = validation["server"] or {}
    if not (bool(server.get("vpn_auto")) and int(server.get("vpn_auto_priority") or 0) >= 0) and not bool(server.get("global_list")):
        return {
            "ok": False,
            "error_code": "SERVER_NOT_USER_SELECTABLE",
            "error_message": (
                "Server is not available in vpn-auto or global-list for user manual selection: "
                f"{server_id}"
            ),
            "server": server,
        }

    return validation


def _audit_server_ref(server_id: str | None) -> str | None:
    value = str(server_id or "").strip()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _event_server_label(connection: Any, server_id: str | None) -> str | None:
    if not server_id:
        return None
    row = connection.execute("SELECT server_name FROM servers WHERE server_id = ?", (server_id,)).fetchone()
    return safe_human_label(row["server_name"]) if row else None


def _enabled_managed_core_intent(connection: Any) -> dict[str, Any] | None:
    """Capture the existing enabled Core intent required by fixed convergence."""
    import json

    from fwrouter_api.services.core_intent import read_core_intent_revision

    row = connection.execute(
        "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
    ).fetchone()
    if row is None or (str(row["desired_state"]), str(row["lifecycle_mode"])) != ("enabled", "managed"):
        return None
    bypass = connection.execute("SELECT value_json FROM settings WHERE key='core.bypass'").fetchone()
    if bypass is not None:
        try:
            decoded = json.loads(str(bypass["value_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict) or type(decoded.get("enabled", False)) is not bool or decoded.get("enabled"):
            return None
    try:
        revision = read_core_intent_revision(connection)
    except ValueError:
        return None
    snapshot = tuple(str(row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
    return {"revision": revision, "snapshot": snapshot}


def _capture_fixed_apply_context(routing: dict[str, Any]) -> dict[str, Any] | None:
    from fwrouter_api.services.core_intent import read_core_intent_revision, read_core_runtime_context
    from fwrouter_api.services.mihomo_config_paths import _resolved_base_config_path
    from fwrouter_api.services.mihomo_reconcile import _file_hash, current_mihomo_input_fingerprint
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    core_context = read_core_runtime_context()
    incarnation = get_mihomo_runtime_incarnation(timeout_seconds=5.0)
    if not core_context or not incarnation:
        return None
    with db_session() as connection:
        revision = read_selection_revision(connection)
        core_revision = read_core_intent_revision(connection)
        core_row = connection.execute(
            "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'"
        ).fetchone()
        if core_row is None or (str(core_row["desired_state"]), str(core_row["lifecycle_mode"])) != ("enabled", "managed"):
            return None
        core_snapshot = tuple(str(core_row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
        from fwrouter_api.services.xray_vpn_auto_pending import _load_row
        pending = _load_row(connection)
        workflow = pending.get("workflow") if isinstance(pending, dict) else None
        if (
            not isinstance(pending, dict) or pending.get("pending") is not True
            or not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server"
            or workflow.get("selection_revision") != revision
            or workflow.get("server_id") != routing.get("desired_fixed_server_id")
            or workflow.get("fixed_until") != routing.get("fixed_server_until")
        ):
            return None
        pending_revision = pending.get("revision")
        if type(pending_revision) is not int:
            return None
        phase = str(workflow.get("phase") or "")
        generation_reservation = workflow.get("mihomo_generation_reservation")
        generation_receipt = workflow.get("mihomo_generation_receipt")
        if phase in {"mihomo_reserved", "mihomo_generation_verified", "mihomo_verified", "core_verified", "xray_verified"}:
            expected_revision = (
                generation_reservation.get("selection_revision")
                if isinstance(generation_reservation, dict) else None
            )
            if phase == "mihomo_reserved":
                if (
                    not isinstance(generation_reservation, dict)
                    or type(expected_revision) is not int or expected_revision > revision
                    or type(generation_reservation.get("before_revision")) is not int
                    or generation_reservation.get("before_revision") + 1 != expected_revision
                    or workflow.get("selection_revision") != revision
                    or not isinstance(generation_reservation.get("operation_id"), str)
                ):
                    return None
            elif isinstance(generation_reservation, dict):
                if (
                    type(expected_revision) is not int or expected_revision > revision
                    or workflow.get("selection_revision") != revision
                    or not _fixed_generation_receipt_valid(generation_reservation, generation_receipt)
                ):
                    return None
            elif not (
                phase in {"mihomo_verified", "core_verified", "xray_verified"}
                and workflow.get("mihomo_generation_mode") == "unchanged"
                and workflow.get("selection_revision") == revision
            ):
                return None
        elif phase != "intent":
            return None
    fingerprint = current_mihomo_input_fingerprint(routing)
    if not isinstance(fingerprint, dict) or not fingerprint.get("hash"):
        return None
    token = {
        "selection_revision": revision,
        "pending_revision": pending_revision,
        "core_revision": core_revision,
        "core_snapshot": core_snapshot,
        "core_runtime_context": core_context,
        "runtime_incarnation": incarnation,
        "input_hash": str(fingerprint["hash"]),
        "active_config_hash": _file_hash(_resolved_base_config_path()),
    }
    if phase != "intent":
        token["fixed_workflow_phase"] = phase
    if isinstance(generation_reservation, dict):
        token["mihomo_generation_reservation"] = dict(generation_reservation)
        token["source_selection_revision"] = int(generation_reservation["before_revision"])
    if isinstance(generation_receipt, dict):
        token["mihomo_generation_receipt"] = dict(generation_receipt)
    return token


def _capture_global_auto_apply_context(routing: dict[str, Any]) -> dict[str, Any] | None:
    """Capture the existing auto-apply fences without requiring a fixed marker."""
    from fwrouter_api.services.core_intent import read_core_intent_revision, read_core_runtime_context
    from fwrouter_api.services.mihomo_config_paths import _resolved_base_config_path
    from fwrouter_api.services.mihomo_reconcile import _file_hash, current_mihomo_input_fingerprint
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    core_context = read_core_runtime_context()
    incarnation = get_mihomo_runtime_incarnation(timeout_seconds=5.0)
    if not core_context or not incarnation:
        return None
    with db_session() as connection:
        revision = read_selection_revision(connection)
        core_revision = read_core_intent_revision(connection)
        core_row = connection.execute(
            "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
        ).fetchone()
        if core_row is None:
            return None
        core_snapshot = tuple(str(core_row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
    fingerprint = current_mihomo_input_fingerprint(routing)
    if not isinstance(fingerprint, dict) or not fingerprint.get("hash"):
        return None
    return {
        "selection_revision": revision,
        "core_revision": core_revision,
        "core_snapshot": core_snapshot,
        "core_runtime_context": core_context,
        "runtime_incarnation": incarnation,
        "input_hash": str(fingerprint["hash"]),
        "active_config_hash": _file_hash(_resolved_base_config_path()),
    }


def _fixed_apply_token_matches(
    *,
    server_id: str,
    fixed_until: str,
    token: dict[str, Any],
    expected_selection_revision: int | None = None,
    require_runtime_identity: bool = True,
    require_active_config: bool = True,
) -> bool:
    from fwrouter_api.services.core_intent import (
        assert_core_intent_snapshot_current,
        read_core_runtime_context,
    )
    from fwrouter_api.services.mihomo_config_paths import _resolved_base_config_path
    from fwrouter_api.services.mihomo_reconcile import _file_hash, current_mihomo_input_fingerprint
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    try:
        with db_session() as connection:
            row = connection.execute(
                """SELECT desired_mode, applied_mode, selective_default, server_mode,
                          desired_fixed_server_id, applied_fixed_server_id, fixed_server_until,
                          active_auto_server_id, apply_state, error_code, error_message, updated_at,
                          datetime(fixed_server_until) IS NOT NULL
                            AND datetime(fixed_server_until) > CURRENT_TIMESTAMP AS ttl_live
                   FROM routing_global_state WHERE id=1"""
            ).fetchone()
            expected_revision = (
                token.get("selection_revision")
                if expected_selection_revision is None
                else expected_selection_revision
            )
            if row is None or read_selection_revision(connection) != expected_revision:
                return False
            from fwrouter_api.services.xray_vpn_auto_pending import _load_row
            pending = _load_row(connection)
            workflow = pending.get("workflow") if isinstance(pending, dict) else None
            if (
                not isinstance(pending, dict) or pending.get("pending") is not True
                or pending.get("revision") != token.get("pending_revision")
                or not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server"
                or workflow.get("selection_revision") != expected_revision
                or workflow.get("server_id") != server_id
                or workflow.get("fixed_until") != fixed_until
            ):
                return False
            if not assert_core_intent_snapshot_current(
                connection,
                int(token["core_revision"]),
                tuple(token["core_snapshot"]),
            ):
                return False
            if (
                str(row["server_mode"] or "auto") != "fixed"
                or str(row["desired_fixed_server_id"] or "") != server_id
                or str(row["fixed_server_until"] or "") != fixed_until
                or str(row["apply_state"] or "") not in {"pending", "applying", "clean"}
                or row["error_code"] is not None
                or row["error_message"] is not None
                or not bool(row["ttl_live"])
            ):
                return False
            persisted_routing = {
                "desired_mode": str(row["desired_mode"] or "direct"),
                "applied_mode": row["applied_mode"],
                "selective_default": str(row["selective_default"] or "direct"),
                "server_mode": str(row["server_mode"] or "auto"),
                # Keep nullable intent fields byte-for-byte equivalent to the
                # routing snapshot used when the input fingerprint was bound.
                # Converting SQLite NULL to an empty string changes that
                # fingerprint and makes every legitimate fixed token stale.
                "desired_fixed_server_id": row["desired_fixed_server_id"],
                "applied_fixed_server_id": row["applied_fixed_server_id"],
                "fixed_server_until": row["fixed_server_until"],
                "active_auto_server_id": row["active_auto_server_id"],
                "apply_state": str(row["apply_state"] or ""),
                "error_code": row["error_code"],
                "error_message": row["error_message"],
                "updated_at": row["updated_at"],
            }
        current_input = current_mihomo_input_fingerprint(persisted_routing)
        return bool(
            read_core_runtime_context() == token.get("core_runtime_context")
            and (
                not require_runtime_identity
                or get_mihomo_runtime_incarnation(timeout_seconds=5.0) == token.get("runtime_incarnation")
            )
            and (
                not require_active_config
                or _file_hash(_resolved_base_config_path()) == token.get("active_config_hash")
            )
            and isinstance(current_input, dict)
            and current_input.get("hash") == token.get("input_hash")
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _fixed_materialization_callback(routing: dict[str, Any], token: dict[str, Any]):
    from fwrouter_api.services.mihomo_config_status import mihomo_runtime_fixed_target_materialized

    def verify(*, operation_id: str, expected_selection_revision: int) -> dict[str, Any]:
        original_revision = token.get("selection_revision")
        revision_owned = expected_selection_revision in {
            original_revision,
            original_revision + 1 if type(original_revision) is int else None,
        }
        intent_current = bool(
            revision_owned
            and _fixed_apply_token_matches(
                server_id=str(routing.get("desired_fixed_server_id") or ""),
                fixed_until=str(routing.get("fixed_server_until") or ""),
                token=token,
                expected_selection_revision=expected_selection_revision,
                require_runtime_identity=False,
                require_active_config=False,
            )
        )
        result = (
            mihomo_runtime_fixed_target_materialized(routing)
            if intent_current
            else {"ok": False, "reason": "fixed_intent_superseded"}
        )
        return {
            **result,
            "ok": bool(result.get("ok") and intent_current),
            "operation_id": operation_id,
            "selection_revision": expected_selection_revision,
            "selected_fixed_target": False,
        }

    return verify


def _fixed_generation_callbacks(routing: dict[str, Any], token: dict[str, Any]):
    """Bind reconcile reservation and verified publication to this fixed owner."""
    from fwrouter_api.services.core_intent import assert_core_intent_snapshot_current
    from fwrouter_api.services.mihomo_reconcile import _fixed_generation_checkpoint_matches
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row, _save_if_revision

    server_id = str(routing.get("desired_fixed_server_id") or "")
    fixed_until = str(routing.get("fixed_server_until") or "")
    pending_revision = int(token.get("pending_revision") or -1)
    source_revision = int(token.get("source_selection_revision", token.get("selection_revision", -1)))

    def _owned_row(connection: Any, selection_revision: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
        pending = _load_row(connection)
        workflow = pending.get("workflow") if isinstance(pending, dict) else None
        route = connection.execute(
            """SELECT server_mode, desired_fixed_server_id, fixed_server_until, apply_state,
                      error_code, error_message,
                      datetime(fixed_server_until) IS NOT NULL
                        AND datetime(fixed_server_until)>CURRENT_TIMESTAMP AS ttl_live
               FROM routing_global_state WHERE id=1""",
        ).fetchone()
        if (
            not isinstance(pending, dict) or pending.get("pending") is not True
            or pending.get("revision") != pending_revision
            or not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server"
            or workflow.get("server_id") != server_id or workflow.get("fixed_until") != fixed_until
            or workflow.get("core_revision") != token.get("core_revision")
            or workflow.get("core_snapshot") != list(token.get("core_snapshot") or ())
            or not assert_core_intent_snapshot_current(
                connection, int(token["core_revision"]), tuple(token["core_snapshot"]),
            )
            or route is None or route["server_mode"] != "fixed"
            or route["desired_fixed_server_id"] != server_id
            or route["fixed_server_until"] != fixed_until
            or route["apply_state"] not in {"pending", "applying", "clean"}
            or route["error_code"] is not None or route["error_message"] is not None
            or not bool(route["ttl_live"])
            or read_selection_revision(connection) != selection_revision
        ):
            return None
        return pending, workflow

    def reserve_or_validate(
        *, mode: str, reservation: dict[str, Any], expected_selection_revision: int,
        **proof: Any,
    ) -> dict[str, Any]:
        if not xray_writer_guard_is_held():
            return {"ok": False}
        operation_id = reservation.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            return {"ok": False}
        if not _fixed_generation_checkpoint_matches(operation_id, reservation.get("checkpoint_ref")):
            return {"ok": False}
        if mode == "validate":
            if (
                expected_selection_revision != reservation.get("selection_revision")
                or reservation.get("before_revision") != source_revision
                or proof.get("candidate_config_hash") != reservation.get("candidate_config_hash")
                or proof.get("input_fingerprint_hash") != reservation.get("input_fingerprint_hash")
                or proof.get("active_config_hash") not in {
                    reservation.get("before_active_config_hash"),
                    reservation.get("candidate_config_hash"),
                }
                or not proof.get("runtime_incarnation")
            ):
                return {"ok": False}
            with db_session() as connection:
                owned = _owned_row(connection, expected_selection_revision)
                if owned is None:
                    return {"ok": False}
                _pending, workflow = owned
                if (
                    workflow.get("phase") != "mihomo_reserved"
                    or workflow.get("selection_revision") != expected_selection_revision
                    or workflow.get("mihomo_generation_reservation") != reservation
                ):
                    return {"ok": False}
            return {"ok": True, "before_revision": source_revision,
                    "selection_revision": expected_selection_revision}

        if mode != "reserve" or expected_selection_revision != source_revision:
            return {"ok": False}
        if (
            reservation.get("schema") != "mihomo-fixed-generation-reservation/v1"
            or reservation.get("before_revision") != source_revision
            or reservation.get("selection_revision") != source_revision + 1
            or reservation.get("candidate_config_hash") is None
            or reservation.get("input_fingerprint_hash") is None
            or reservation.get("before_active_config_hash") != token.get("active_config_hash")
            or reservation.get("before_runtime_incarnation") != token.get("runtime_incarnation")
        ):
            return {"ok": False}
        with db_session() as connection:
            owned = _owned_row(connection, source_revision)
            if owned is None:
                return {"ok": False}
            pending, workflow = owned
            if (
                workflow.get("phase") != "intent"
                or workflow.get("selection_revision") != source_revision
                or workflow.get("mihomo_generation_reservation") is not None
            ):
                return {"ok": False}
            reserved_revision = advance_selection_revision(
                connection, expected_revision=source_revision,
            )
            if reserved_revision != source_revision + 1:
                connection.rollback()
                return {"ok": False}
            workflow.update({
                "phase": "mihomo_reserved",
                "selection_revision": reserved_revision,
                "mihomo_generation_reservation": reservation,
            })
            pending["workflow"] = workflow
            if not _save_if_revision(connection, pending, pending_revision):
                connection.rollback()
                return {"ok": False}
        token.update({
            "source_selection_revision": source_revision,
            "selection_revision": reserved_revision,
            "mihomo_generation_reservation": dict(reservation),
            "fixed_workflow_phase": "mihomo_reserved",
        })
        return {"ok": True, "before_revision": source_revision,
                "selection_revision": reserved_revision}

    def publish_verified_receipt(
        *, operation_id: str, expected_selection_revision: int,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        reservation = token.get("mihomo_generation_reservation")
        if (
            not xray_writer_guard_is_held()
            or not isinstance(reservation, dict)
            or operation_id != reservation.get("operation_id")
            or expected_selection_revision != reservation.get("selection_revision")
            or receipt.get("operation_id") != operation_id
            or receipt.get("before_revision") != reservation.get("before_revision")
            or receipt.get("selection_revision") != expected_selection_revision
            or receipt.get("input_fingerprint_hash") != reservation.get("input_fingerprint_hash")
            or receipt.get("candidate_config_hash") != reservation.get("candidate_config_hash")
            or receipt.get("active_config_hash") not in {
                reservation.get("candidate_config_hash"),
                reservation.get("before_active_config_hash"),
            }
            or receipt.get("schema") != "mihomo-fixed-generation-receipt/v1"
            or not isinstance(receipt.get("runtime_incarnation"), str)
            or not receipt.get("runtime_incarnation")
            or receipt.get("native_udp_policy_verified") is not True
            or receipt.get("native_top_level_rules_match") is not True
            or receipt.get("native_selector_membership_matches") is not True
            or receipt.get("guarded_rules_match") is not True
            or receipt.get("publication_verified") is not True
        ):
            return {"ok": False}
        with db_session() as connection:
            owned = _owned_row(connection, expected_selection_revision)
            if owned is None:
                return {"ok": False}
            pending, workflow = owned
            if (
                workflow.get("phase") != "mihomo_reserved"
                or workflow.get("selection_revision") != expected_selection_revision
                or workflow.get("mihomo_generation_reservation") != reservation
                or workflow.get("mihomo_generation_receipt") is not None
            ):
                return {"ok": False}
            workflow.update({
                "phase": "mihomo_generation_verified",
                "mihomo_generation_receipt": receipt,
            })
            pending["workflow"] = workflow
            if not _save_if_revision(connection, pending, pending_revision):
                connection.rollback()
                return {"ok": False}
        token["mihomo_generation_receipt"] = dict(receipt)
        token["fixed_workflow_phase"] = "mihomo_generation_verified"
        return {"ok": True}

    return reserve_or_validate, publish_verified_receipt


def _materialize_fixed_server_generation(
    routing: dict[str, Any], *, token: dict[str, Any], job_id: str,
) -> dict[str, Any]:
    """Revalidate the captured fixed intent and serialize its generation owner."""
    from fwrouter_api.services.mihomo_reconcile import reconcile_mihomo_runtime

    phase = str(token.get("fixed_workflow_phase") or "intent")
    if phase in {"mihomo_generation_verified", "mihomo_verified", "core_verified", "xray_verified"}:
        from fwrouter_api.services.mihomo_config_status import mihomo_runtime_fixed_target_materialized
        from fwrouter_api.services.mihomo_reconcile_fingerprint import mihomo_udp_policy_receipt_matches

        reservation = token.get("mihomo_generation_reservation")
        receipt = token.get("mihomo_generation_receipt")
        if phase == "mihomo_generation_verified" and (
            not _fixed_generation_receipt_valid(reservation, receipt)
            or receipt.get("runtime_incarnation") != token.get("runtime_incarnation")
            or receipt.get("active_config_hash") != token.get("active_config_hash")
            or token.get("selection_revision") != receipt.get("selection_revision")
        ):
            return {"ok": False, "stage": "generation_resume_receipt", "error_code": "MIHOMO_GENERATION_RECEIPT_INVALID"}
        # A recorded Xray generation may have recreated Mihomo at REJECT.
        # Its fixed selection is restored by the still-pending fixed owner.
        selected = phase in {"mihomo_verified", "core_verified"}
        proof = mihomo_runtime_fixed_target_materialized(routing, require_selected=selected)
        if (
            not proof.get("ok")
            or not mihomo_udp_policy_receipt_matches()
            or not _fixed_apply_token_matches(
                server_id=str(routing.get("desired_fixed_server_id") or ""),
                fixed_until=str(routing.get("fixed_server_until") or ""),
                token=token,
            )
        ):
            return {
                "ok": False, "stage": "generation_resume_readback",
                "error_code": "MIHOMO_GENERATION_READBACK_UNVERIFIED",
                "fixed_materialization_proof": proof,
                "last_good_retained": True,
            }
        if isinstance(receipt, dict):
            operation_id = receipt.get("operation_id")
            before_revision = receipt.get("before_revision")
            selected_revision = receipt.get("selection_revision")
            active_hash = receipt.get("active_config_hash")
            incarnation = receipt.get("runtime_incarnation")
        else:
            operation_id = "unchanged"
            before_revision = token.get("source_selection_revision", token.get("selection_revision"))
            selected_revision = before_revision
            active_hash = token.get("active_config_hash")
            incarnation = token.get("runtime_incarnation")
        return {
            "ok": True,
            "operation_id": operation_id,
            "job_id": receipt.get("job_id") if isinstance(receipt, dict) else job_id,
            "reentry_generation_fence": {
                "schema": "mihomo-reentry-generation-fence/v1",
                "operation_id": operation_id,
                "job_id": receipt.get("job_id") if isinstance(receipt, dict) else job_id,
                "before_revision": before_revision,
                "selection_revision": selected_revision,
                "runtime_incarnation": incarnation,
                "active_config_hash": active_hash,
                "native_proof_verified": True,
                "publication_verified": True,
                "unchanged_config": not isinstance(receipt, dict),
            },
            "last_good_retained": True,
            "resume_readback": proof,
        }

    server_id = str(routing.get("desired_fixed_server_id") or "")
    fixed_until = str(routing.get("fixed_server_until") or "")
    with xray_writer_guard(timeout_seconds=30.0):
        if not _fixed_apply_token_matches(
            server_id=server_id,
            fixed_until=fixed_until,
            token=token,
        ):
            return {
                "ok": False,
                "stage": "fixed_intent_revalidation",
                "error_code": "GLOBAL_FIXED_SERVER_STALE",
                "error_message": "Fixed intent changed before managed generation began.",
                "last_good_retained": True,
            }
        reserve_generation, publish_generation = _fixed_generation_callbacks(routing, token)
        reservation = token.get("mihomo_generation_reservation")
        return reconcile_mihomo_runtime(
            routing=routing,
            job_id=job_id,
            verification_callback=_fixed_materialization_callback(routing, token),
            generation_materialization_callback=_fixed_materialization_callback(routing, token),
            generation_reservation_callback=reserve_generation,
            generation_completion_callback=publish_generation,
            fixed_generation_reservation=(reservation if isinstance(reservation, dict) else None),
        )


def _fixed_workflow_current(revision: int) -> dict[str, Any] | None:
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    with db_session() as connection:
        row = _load_row(connection)
    workflow = row.get("workflow") if isinstance(row, dict) else None
    if (
        not isinstance(row, dict) or row.get("pending") is not True
        or type(row.get("revision")) is not int or row["revision"] != revision
        or not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server"
    ):
        return None
    return {**workflow, "pending_revision": revision}


def _fixed_generation_receipt_valid(reservation: Any, receipt: Any) -> bool:
    if not isinstance(reservation, dict) or not isinstance(receipt, dict):
        return False
    from fwrouter_api.services.mihomo_reconcile import _fixed_generation_checkpoint_matches

    digest = lambda value: isinstance(value, str) and len(value) == 64 and all(  # noqa: E731
        character in "0123456789abcdef" for character in value
    )
    operation_id = reservation.get("operation_id")
    return bool(
        reservation.get("schema") == "mihomo-fixed-generation-reservation/v1"
        and isinstance(operation_id, str)
        and type(reservation.get("before_revision")) is int
        and type(reservation.get("selection_revision")) is int
        and reservation.get("before_revision") + 1 == reservation.get("selection_revision")
        and _fixed_generation_checkpoint_matches(operation_id, reservation.get("checkpoint_ref"))
        and receipt.get("schema") == "mihomo-fixed-generation-receipt/v1"
        and receipt.get("operation_id") == operation_id
        and receipt.get("before_revision") == reservation.get("before_revision")
        and receipt.get("selection_revision") == reservation.get("selection_revision")
        and receipt.get("input_fingerprint_hash") == reservation.get("input_fingerprint_hash")
        and receipt.get("candidate_config_hash") == reservation.get("candidate_config_hash")
        and digest(reservation.get("candidate_config_hash"))
        and digest(receipt.get("active_config_hash"))
        and receipt.get("active_config_hash") in {
            reservation.get("candidate_config_hash"),
            reservation.get("before_active_config_hash"),
        }
        and isinstance(receipt.get("runtime_incarnation"), str)
        and bool(receipt.get("runtime_incarnation"))
        and receipt.get("native_udp_policy_verified") is True
        and receipt.get("native_top_level_rules_match") is True
        and receipt.get("native_selector_membership_matches") is True
        and receipt.get("guarded_rules_match") is True
        and receipt.get("publication_verified") is True
    )


def _refresh_fixed_token_after_core_apply(
    *, server_id: str, fixed_until: str, token: dict[str, Any],
) -> bool:
    """Bind the token to the Core owner's committed routing projection.

    Selection revision, Core intent and Mihomo runtime incarnation remain
    invariant. Only the persisted applied-mode projection may change as part
    of the owned Core apply, so recompute its input fingerprint after proving
    those fences still belong to this workflow.
    """
    from fwrouter_api.services.core_intent import read_core_runtime_context, read_core_intent_revision
    from fwrouter_api.services.mihomo_config_paths import _resolved_base_config_path
    from fwrouter_api.services.mihomo_reconcile import _file_hash, current_mihomo_input_fingerprint
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row

    routing = get_routing_global_state(expire_ttl=False)
    if not isinstance(routing, dict):
        return False
    with db_session() as connection:
        row = connection.execute(
            "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
        ).fetchone()
        pending = _load_row(connection)
        workflow = pending.get("workflow") if isinstance(pending, dict) else None
        if (
            read_selection_revision(connection) != token.get("selection_revision")
            or read_core_intent_revision(connection) != token.get("core_revision")
            or row is None
            or tuple(str(row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
                != tuple(token.get("core_snapshot") or ())
            or not isinstance(pending, dict) or pending.get("pending") is not True
            or pending.get("revision") != token.get("pending_revision")
            or not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server"
            or workflow.get("phase") != "core_verified"
            or workflow.get("server_id") != server_id
        ):
            return False
    if (
        routing.get("server_mode") != "fixed"
        or routing.get("desired_fixed_server_id") != server_id
        or routing.get("applied_fixed_server_id") != server_id
        or routing.get("fixed_server_until") != fixed_until
        or routing.get("apply_state") != "clean"
        or read_core_runtime_context() != token.get("core_runtime_context")
        or get_mihomo_runtime_incarnation(timeout_seconds=5.0) != token.get("runtime_incarnation")
        or _file_hash(_resolved_base_config_path()) != token.get("active_config_hash")
    ):
        return False
    fingerprint = current_mihomo_input_fingerprint(routing)
    if not isinstance(fingerprint, dict) or not isinstance(fingerprint.get("hash"), str):
        return False
    token["input_hash"] = str(fingerprint["hash"])
    return True


def _update_fixed_workflow(revision: int, **changes: Any) -> dict[str, Any] | None:
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row, _save_if_revision

    with db_session() as connection:
        state = _load_row(connection)
        if (
            not isinstance(state, dict) or state.get("pending") is not True
            or type(state.get("revision")) is not int or state["revision"] != revision
        ):
            return None
        workflow = state.get("workflow")
        if not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server":
            return None
        workflow = {**workflow, **changes}
        state["workflow"] = workflow
        if not _save_if_revision(connection, state, revision):
            return None
    return {**workflow, "pending_revision": revision}


def _fixed_xray_readback() -> dict[str, Any]:
    """Classify Xray source/artifact/config/runtime parity without changing it."""
    import hashlib
    from pathlib import Path

    from fwrouter_api.services.xray_bindings import (
        collect_xray_runtime_bindings, collect_xray_client_mode_directives,
    )
    from fwrouter_api.services.xray_materialize import (
        _verify_active_config_bindings, _verify_active_config_client_modes,
    )
    from fwrouter_api.services.xray_runtime_state import _module_state
    from fwrouter_api.services.xray_subscription_service import _current_bindings_artifact_parity
    from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER

    module = _module_state("xray") or {}
    bindings = collect_xray_runtime_bindings()
    modes = collect_xray_client_mode_directives()
    desired_identities = sorted(
        (str(item.get("client_uuid") or item.get("client_id") or ""), str(item.get("client_email") or ""))
        for item in [*bindings, *modes]
    )
    if any(not client_id or not email for client_id, email in desired_identities):
        return {"state": "unknown", "reason": "xray_source_identity_incomplete"}
    desired_identities = sorted(set(desired_identities))
    managed = (
        str(module.get("desired_state") or "") == "enabled"
        and str(module.get("lifecycle_mode") or "") == "managed"
    )
    if not managed and not desired_identities:
        return {"state": "not_applicable", "ok": True}
    if not managed:
        return {"state": "unknown", "reason": "xray_module_not_managed_enabled"}

    adapter = DEFAULT_XRAY_ADAPTER
    try:
        health = adapter.health()
        runtime_state = str(getattr(health, "runtime_state", "") or "").lower()
        if runtime_state.endswith(".running"):
            runtime_state = "running"
        if runtime_state != "running":
            return {"state": "unknown", "reason": "xray_runtime_not_running"}
        config_path = Path(adapter.config_path)
        active_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
        incarnation = adapter.get_runtime_incarnation()
        mounted_hash = adapter.get_runtime_config_sha256()
        loaded = sorted((str(pair[0]), str(pair[1])) for pair in adapter.list_loaded_client_identities())
    except (OSError, AttributeError, TypeError, ValueError, RuntimeError):
        return {"state": "unknown", "reason": "xray_runtime_readback_unavailable"}
    if not incarnation or not mounted_hash:
        return {"state": "unknown", "reason": "xray_runtime_fence_unavailable"}

    artifact_ok, artifact_hash = _current_bindings_artifact_parity(bindings, modes)
    if not artifact_hash:
        return {"state": "unknown", "reason": "xray_bindings_artifact_unavailable"}
    binding_readback = _verify_active_config_bindings(bindings)
    mode_readback = _verify_active_config_client_modes(modes)
    if (
        not isinstance(binding_readback, dict) or not isinstance(mode_readback, dict)
        or binding_readback.get("status") not in {"verified", "failed"}
        or mode_readback.get("status") not in {"verified", "failed"}
        or binding_readback.get("reason") or mode_readback.get("reason")
        or binding_readback.get("identity_mismatch") is not None
        or binding_readback.get("missing_clients")
        or mode_readback.get("missing_clients")
        or loaded != desired_identities
    ):
        return {
            "state": "unknown",
            "reason": "xray_identity_or_config_readback_unproven",
            "identity_count": len(desired_identities),
            "loaded_identity_count": len(loaded),
        }
    binding_known_drift = bool(
        binding_readback.get("status") == "failed"
        and any(binding_readback.get(key) for key in (
            "missing_outbounds", "invalid_handoffs", "missing_rules",
            "wrong_api_rules", "wrong_first_match_rules",
        ))
    )
    mode_known_drift = bool(
        mode_readback.get("status") == "failed"
        and any(mode_readback.get(key) for key in (
            "missing_outbounds", "missing_rules", "misordered_rules",
        ))
    )
    if (
        (binding_readback.get("status") == "verified" and binding_readback.get("ok") is not True)
        or (mode_readback.get("status") == "verified" and mode_readback.get("ok") is not True)
        or (binding_readback.get("status") == "failed" and not binding_known_drift)
        or (mode_readback.get("status") == "failed" and not mode_known_drift)
    ):
        return {"state": "unknown", "reason": "xray_config_readback_unclassified"}
    known_drift = bool(not artifact_ok or active_hash != mounted_hash or binding_known_drift or mode_known_drift)
    return {
        "state": "drift" if known_drift else "verified",
        "ok": not known_drift,
        "active_config_sha256": active_hash,
        "runtime_config_sha256": mounted_hash,
        "runtime_incarnation": str(incarnation),
        "artifact_sha256": artifact_hash,
        "identity_count": len(desired_identities),
        "loaded_identity_count": len(loaded),
    }


def _fixed_core_readback() -> dict[str, Any]:
    from fwrouter_api.services.xray_subscription_service import _collect_legacy_admission_core_native_proof

    return _collect_legacy_admission_core_native_proof()


def _fixed_xray_generation_callback(
    *, server_id: str, mihomo_target: str, fixed_until: str, token: dict[str, Any],
):
    """Bind an Xray generation's Mihomo restart receipt to this fixed intent."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.core_intent import read_core_intent_revision, read_core_runtime_context
    from fwrouter_api.services.mihomo_config_paths import _resolved_base_config_path
    from fwrouter_api.services.mihomo_config_status import mihomo_runtime_fixed_target_materialized
    from fwrouter_api.services.mihomo_reconcile import _file_hash, current_mihomo_input_fingerprint
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row, _save_if_revision

    def verify(*, operation_id: str, expected_selection_revision: int) -> dict[str, Any]:
        if (
            not isinstance(operation_id, str) or not operation_id
            or type(expected_selection_revision) is not int
            or not isinstance(mihomo_target, str) or not mihomo_target
        ):
            return {"ok": False, "error_code": "FIXED_XRAY_GENERATION_RECEIPT_INVALID"}
        with xray_writer_guard(timeout_seconds=30.0):
            routing = get_routing_global_state(expire_ttl=False)
            if not isinstance(routing, dict):
                return {"ok": False, "error_code": "FIXED_XRAY_ROUTING_UNAVAILABLE"}
            # Xray-owned Mihomo restart verification is a membership proof at
            # the fail-closed REJECT selector state. The fixed job restores its
            # captured target after this generation is durably published.
            native = mihomo_runtime_fixed_target_materialized(routing)
            core_context = read_core_runtime_context()
            runtime_incarnation = get_mihomo_runtime_incarnation(timeout_seconds=5.0)
            config_hash = _file_hash(_resolved_base_config_path())
            input_fingerprint = current_mihomo_input_fingerprint(routing)
            if (
                not native.get("ok") or native.get("fixed_runtime_target") != mihomo_target
                or native.get("vpn_global_now") != "REJECT"
                or native.get("selected_fixed_target") is not False
                or routing.get("server_mode") != "fixed"
                or routing.get("desired_fixed_server_id") != server_id
                or routing.get("applied_fixed_server_id") != server_id
                or routing.get("fixed_server_until") != fixed_until
                or routing.get("apply_state") != "clean"
                or core_context != token.get("core_runtime_context")
                or not runtime_incarnation
                or not isinstance(config_hash, str) or len(config_hash) != 64
                or not isinstance(input_fingerprint, dict)
                or not isinstance(input_fingerprint.get("hash"), str)
            ):
                return {"ok": False, "error_code": "FIXED_XRAY_MIHOMO_PROOF_FAILED"}
            with db_session() as connection:
                row = connection.execute(
                    "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
                ).fetchone()
                pending = _load_row(connection)
                workflow = pending.get("workflow") if isinstance(pending, dict) else None
                if (
                    read_selection_revision(connection) != expected_selection_revision
                    or read_core_intent_revision(connection) != token.get("core_revision")
                    or row is None
                    or tuple(str(row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
                        != tuple(token.get("core_snapshot") or ())
                    or not isinstance(pending, dict) or pending.get("pending") is not True
                    or pending.get("revision") != token.get("pending_revision")
                    or not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server"
                    or workflow.get("phase") != "core_verified"
                    or workflow.get("server_id") != server_id
                    or workflow.get("fixed_until") != fixed_until
                    or (
                        workflow.get("selection_revision") != token.get("selection_revision")
                        and not (
                            workflow.get("xray_generation_operation_id") == operation_id
                            and workflow.get("selection_revision") == expected_selection_revision
                        )
                    )
                    or expected_selection_revision < int(token.get("selection_revision") or -1)
                ):
                    return {"ok": False, "error_code": "FIXED_XRAY_WORKFLOW_FENCE_CHANGED"}
                receipt = {
                    "kind": "global_fixed_server",
                    "phase": "core_verified",
                    "selection_revision": expected_selection_revision,
                    "server_id": server_id,
                    "fixed_until": fixed_until,
                    "fixed_pending_revision": int(token["pending_revision"]),
                    "core_revision": token["core_revision"],
                    "core_snapshot": list(token["core_snapshot"]),
                    "mihomo_operation_id": operation_id,
                    "mihomo_input_sha256": input_fingerprint["hash"],
                    "mihomo_config_sha256": config_hash,
                    "mihomo_runtime_incarnation": runtime_incarnation,
                }
                workflow.update({
                    "selection_revision": expected_selection_revision,
                    "xray_generation_operation_id": operation_id,
                    "xray_generation_mihomo_input_sha256": input_fingerprint["hash"],
                    "xray_generation_mihomo_config_sha256": config_hash,
                    "xray_generation_mihomo_runtime_incarnation": runtime_incarnation,
                })
                pending["workflow"] = workflow
                if not _save_if_revision(connection, pending, int(token["pending_revision"])):
                    return {"ok": False, "error_code": "FIXED_XRAY_WORKFLOW_CAS_FAILED"}
            token.update({
                "selection_revision": expected_selection_revision,
                "input_hash": str(input_fingerprint["hash"]),
                "active_config_hash": config_hash,
                "runtime_incarnation": runtime_incarnation,
                "xray_generation_operation_id": operation_id,
            })
            return {
                **native,
                **receipt,
                "ok": True,
                "operation_id": operation_id,
                "selection_revision": expected_selection_revision,
            }

    return verify


def _fixed_xray_generation_receipt_matches(
    result: dict[str, Any], *, server_id: str, token: dict[str, Any],
) -> bool:
    verification = result.get("pre_publication_verification")
    if (
        not isinstance(verification, dict)
        or verification.get("ok") is not True
        or not isinstance(verification.get("operation_id"), str)
        or not verification.get("operation_id")
        or verification.get("operation_id") != token.get("xray_generation_operation_id")
        or verification.get("selection_revision") != token.get("selection_revision")
        or verification.get("server_id") != server_id
        or verification.get("fixed_pending_revision") != token.get("pending_revision")
        or verification.get("core_revision") != token.get("core_revision")
        or verification.get("mihomo_input_sha256") != token.get("input_hash")
        or verification.get("mihomo_config_sha256") != token.get("active_config_hash")
        or verification.get("mihomo_runtime_incarnation") != token.get("runtime_incarnation")
    ):
        return False
    generation_apply = result.get("generation_apply")
    final_mihomo = generation_apply.get("final_mihomo") if isinstance(generation_apply, dict) else None
    if isinstance(final_mihomo, dict):
        container = final_mihomo.get("container")
        if not final_mihomo.get("ok"):
            return False
        if isinstance(container, dict) and container.get("selection_revision") is not None:
            if container.get("selection_revision") != token.get("selection_revision"):
                return False
    return True


def _restore_fixed_selection_after_xray_generation(
    *, server_id: str, mihomo_target: str, fixed_until: str,
    token: dict[str, Any], workflow: dict[str, Any],
) -> dict[str, Any]:
    """Re-enter the fixed owner after its recorded Xray generation reset Mihomo."""
    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoApplyResult
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.mihomo_config_status import mihomo_runtime_fixed_target_materialized

    receipt_keys = (
        "phase", "selection_revision", "server_id", "fixed_until",
        "core_apply_id", "core_manifest_hash", "core_enforcement_level", "core_live_mode",
        "xray_generation_operation_id", "xray_generation_mihomo_input_sha256",
        "xray_generation_mihomo_config_sha256", "xray_generation_mihomo_runtime_incarnation",
        "xray_state", "xray_runtime_incarnation", "xray_config_sha256", "xray_artifact_sha256",
    )
    expected_workflow = {key: workflow.get(key) for key in receipt_keys}
    if (
        workflow.get("phase") != "xray_verified"
        or workflow.get("server_id") != server_id
        or workflow.get("fixed_until") != fixed_until
        or not isinstance(workflow.get("xray_generation_operation_id"), str)
        or not workflow.get("xray_generation_operation_id")
        or workflow.get("xray_state") != "verified"
        or workflow.get("xray_generation_mihomo_input_sha256") != token.get("input_hash")
        or workflow.get("xray_generation_mihomo_config_sha256") != token.get("active_config_hash")
        or workflow.get("xray_generation_mihomo_runtime_incarnation") != token.get("runtime_incarnation")
        or token.get("selection_revision") != workflow.get("selection_revision")
    ):
        return {"ok": False, "error_code": "GLOBAL_FIXED_SERVER_STALE"}

    def xray_owner_proof_matches() -> bool:
        proof = _fixed_xray_readback()
        return bool(
            proof.get("state") == workflow.get("xray_state")
            and proof.get("runtime_incarnation") == workflow.get("xray_runtime_incarnation")
            and proof.get("active_config_sha256") == workflow.get("xray_config_sha256")
            and proof.get("artifact_sha256") == workflow.get("xray_artifact_sha256")
        )

    def core_owner_proof_matches(expected_workflow: dict[str, Any]) -> bool:
        expected = {
            "apply_id": expected_workflow.get("core_apply_id"),
            "manifest_hash": expected_workflow.get("core_manifest_hash"),
            "enforcement_level": expected_workflow.get("core_enforcement_level"),
            "live_global_mode": expected_workflow.get("core_live_mode"),
        }
        if any(not isinstance(value, str) or not value for value in expected.values()):
            return False
        proof = _fixed_core_readback()
        return bool(
            proof.get("ok") is True
            and all(proof.get(key) == value for key, value in expected.items())
        )

    with xray_writer_guard(timeout_seconds=30.0):
        current_workflow = _fixed_workflow_current(int(token.get("pending_revision") or -1))
        if (
            current_workflow is None
            or {key: current_workflow.get(key) for key in receipt_keys} != expected_workflow
            or not _fixed_apply_token_matches(
                server_id=server_id, fixed_until=fixed_until, token=token,
            )
            or not xray_owner_proof_matches()
            or not core_owner_proof_matches(expected_workflow)
        ):
            return {"ok": False, "error_code": "GLOBAL_FIXED_SERVER_STALE"}

        routing = get_routing_global_state(expire_ttl=False)
        if not isinstance(routing, dict):
            return {"ok": False, "error_code": "GLOBAL_FIXED_SERVER_STALE"}
        membership = mihomo_runtime_fixed_target_materialized(routing)
        if (
            membership.get("ok") is not True
            or membership.get("fixed_runtime_target") != mihomo_target
            or membership.get("vpn_global_now") not in {"REJECT", mihomo_target}
        ):
            return {"ok": False, "error_code": "GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED"}

        # Recheck Core and the exact durable owner immediately before the
        # selector effect. The first checks above can be separated from this
        # point by native membership readback.
        current_workflow = _fixed_workflow_current(int(token.get("pending_revision") or -1))
        if (
            current_workflow is None
            or {key: current_workflow.get(key) for key in receipt_keys} != expected_workflow
            or not _fixed_apply_token_matches(
                server_id=server_id, fixed_until=fixed_until, token=token,
            )
            or not xray_owner_proof_matches()
            or not core_owner_proof_matches(expected_workflow)
        ):
            return {"ok": False, "error_code": "GLOBAL_FIXED_SERVER_STALE"}

        if membership.get("vpn_global_now") == "REJECT":
            apply_result = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector("vpn-global", mihomo_target)
            if not apply_result.ok:
                return {
                    "ok": False,
                    "error_code": apply_result.error_code or "GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED",
                    "apply_result": apply_result.to_dict(),
                }
        else:
            apply_result = MihomoApplyResult(
                ok=True,
                message="Recorded Xray generation already has the fixed target selected.",
                active_server_id=mihomo_target,
                details={"reentry_without_native_write": True},
            )

        selected = mihomo_runtime_fixed_target_materialized(routing, require_selected=True)
        current_workflow = _fixed_workflow_current(int(token.get("pending_revision") or -1))
        if (
            selected.get("ok") is not True
            or selected.get("fixed_runtime_target") != mihomo_target
            or not _fixed_apply_token_matches(
                server_id=server_id, fixed_until=fixed_until, token=token,
            )
            or current_workflow is None
            or {key: current_workflow.get(key) for key in receipt_keys} != expected_workflow
            or not xray_owner_proof_matches()
            or not core_owner_proof_matches(expected_workflow)
        ):
            return {"ok": False, "error_code": "GLOBAL_FIXED_SERVER_STALE"}
        return {"ok": True, "proof": selected, "apply_result": apply_result}


def _commit_fixed_mihomo_stage(
    *, server_id: str, fixed_until: str, token: dict[str, Any],
) -> dict[str, Any] | None:
    """Publish the already-proven Mihomo selection as an intermediate state."""
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision
    from fwrouter_api.services.xray_vpn_auto_pending import _load_row, _save_if_revision

    pending_revision = int(token.get("pending_revision") or -1)
    generation_reservation = token.get("mihomo_generation_reservation")
    generation_receipt = token.get("mihomo_generation_receipt")
    phase = str(token.get("fixed_workflow_phase") or "intent")
    if phase == "mihomo_generation_verified":
        if not _fixed_generation_receipt_valid(generation_reservation, generation_receipt):
            return None
    elif phase != "intent":
        return None
    with xray_writer_guard(timeout_seconds=30.0), db_session() as connection:
        if not _fixed_apply_token_matches(
            server_id=server_id, fixed_until=fixed_until, token=token,
        ):
            return None
        row = connection.execute(
            "SELECT applied_fixed_server_id, apply_state FROM routing_global_state WHERE id=1",
        ).fetchone()
        if row is None:
            return None
        current_revision = read_selection_revision(connection)
        if row["applied_fixed_server_id"] == server_id and row["apply_state"] == "clean":
            next_revision = current_revision
        else:
            changed = connection.execute(
                """UPDATE routing_global_state SET applied_fixed_server_id=?, apply_state='clean',
                          error_code=NULL, error_message=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=1 AND server_mode='fixed' AND desired_fixed_server_id=?
                     AND fixed_server_until=? AND apply_state IN ('pending','applying','clean')""",
                (server_id, server_id, fixed_until),
            )
            if changed.rowcount != 1:
                return None
            next_revision = advance_selection_revision(
                connection, expected_revision=current_revision,
            )
            if next_revision is None:
                connection.rollback()
                return None
        pending = _load_row(connection)
        workflow = pending.get("workflow") if isinstance(pending, dict) else None
        if (
            not isinstance(pending, dict) or pending.get("pending") is not True
            or pending.get("revision") != pending_revision
            or not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server"
            or workflow.get("phase") != phase
            or workflow.get("selection_revision") != current_revision
            or workflow.get("mihomo_generation_reservation") != generation_reservation
            or workflow.get("mihomo_generation_receipt") != generation_receipt
        ):
            connection.rollback()
            return None
        workflow.update({"phase": "mihomo_verified", "selection_revision": next_revision})
        if generation_reservation is None:
            workflow["mihomo_generation_mode"] = "unchanged"
        pending["workflow"] = workflow
        if not _save_if_revision(connection, pending, pending_revision):
            connection.rollback()
            return None
    current_routing = get_routing_global_state(expire_ttl=False)
    if current_routing is None:
        return None
    from fwrouter_api.services.mihomo_config_paths import _resolved_base_config_path
    from fwrouter_api.services.mihomo_reconcile import _file_hash, current_mihomo_input_fingerprint
    current_input = current_mihomo_input_fingerprint(current_routing)
    if not isinstance(current_input, dict) or not isinstance(current_input.get("hash"), str):
        return None
    with db_session() as connection:
        core = connection.execute(
            "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
        ).fetchone()
    if core is None:
        return None
    token.update({
        "selection_revision": next_revision,
        "fixed_workflow_phase": "mihomo_verified",
        "core_snapshot": tuple(str(core[key]) for key in ("desired_state", "lifecycle_mode", "apply_state")),
        "input_hash": str(current_input["hash"]),
        "active_config_hash": _file_hash(_resolved_base_config_path()),
    })
    return {"routing": current_routing, "selection_revision": next_revision}


@xray_writer_guarded
def set_global_fixed_server(
    server_id: str,
    *,
    requested_by: str = "admin",
    job_id: str | None = None,
    management_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist admin global fixed server desired state.

    Admin global fixed server is selected from full active inventory and is not
    limited by vpn_auto/global_list flags. This function does not switch Mihomo;
    runtime apply is handled by a separate apply step.
    """

    validation = _validate_global_fixed_server(server_id)

    if not validation["ok"]:
        return {
            "ok": False,
            "routing": ensure_routing_global_state(),
            "server": validation["server"],
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
        }

    with db_session() as connection:
        core_intent = _enabled_managed_core_intent(connection)
        if core_intent is None:
            return {
                "ok": False,
                "error_code": "CORE_NOT_ENABLED_MANAGED",
                "error_message": "Core must be enabled and managed before fixed routing can begin.",
                "server": validation["server"],
            }
        previous = connection.execute(
            "SELECT server_mode, desired_fixed_server_id FROM routing_global_state WHERE id = 1"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id,
                desired_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                fixed_server_until,
                apply_state,
                error_code,
                error_message,
                updated_at
            )
            VALUES (1, 'direct', 'direct', 'fixed', ?, datetime('now', '+' || ? || ' hours'), 'pending', NULL, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                server_mode = 'fixed',
                desired_fixed_server_id = excluded.desired_fixed_server_id,
                fixed_server_until = excluded.fixed_server_until,
                apply_state = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (server_id, GLOBAL_FIXED_SERVER_TTL_HOURS),
        )
        previous_mode = str(previous["server_mode"] or "auto") if previous else "auto"
        previous_server_id = str(previous["desired_fixed_server_id"] or "") if previous else ""
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision
        current_revision = read_selection_revision(connection)
        selection_revision = advance_selection_revision(connection, expected_revision=current_revision)
        if selection_revision is None:
            raise RuntimeError("Fixed-server intent changed while its selection fence was being reserved.")
        from fwrouter_api.services.xray_vpn_auto_pending import mark_xray_vpn_auto_pending
        pending = mark_xray_vpn_auto_pending(
            connection,
            trigger="global_fixed_server",
            immediate=True,
            workflow={
                "kind": "global_fixed_server",
                "phase": "intent",
                "owner_job_id": job_id,
                "requested_by": requested_by,
                "management_context": {
                    key: str(value)[:160]
                    for key, value in (management_context or {}).items()
                    if key in {"source_type", "client_name", "channel", "actor", "action", "request_id"}
                    and value is not None
                },
                "server_id": server_id,
                "fixed_until": connection.execute(
                    "SELECT fixed_server_until FROM routing_global_state WHERE id=1",
                ).fetchone()["fixed_server_until"],
                "selection_revision": selection_revision,
                "core_revision": core_intent["revision"],
                "core_snapshot": list(core_intent["snapshot"]),
            },
            supersede_fixed_workflow=True,
        )
        if previous_mode != "fixed" or previous_server_id != server_id:
            from fwrouter_api.services.event_contract import current_event_context
            from fwrouter_api.services.events import create_event_context, write_audit_event

            entity_id = "global"
            write_audit_event(
                actor=requested_by,
                actor_attribution="caller_supplied",
                source="routing_admin_api",
                action="global_fixed_server_changed",
                event_code="routing.global_fixed_server_changed",
                legacy_event_type="routing.global_fixed_server_changed",
                entity_type="routing",
                entity_id=entity_id,
                previous_value={
                    "server_mode": previous_mode,
                    "server_ref": _audit_server_ref(previous_server_id),
                    "server_label": _event_server_label(connection, previous_server_id),
                },
                new_value={
                    "server_mode": "fixed", "server_ref": _audit_server_ref(server_id),
                    "server_label": safe_human_label(validation["server"].get("server_name")),
                },
                context=create_event_context(
                    request_id=current_event_context().get("request_id"),
                    job_id=job_id,
                    entity_id=entity_id,
                ),
                details={"outcome": "intent_committed"},
                connection=connection,
            )

    return {
        "ok": True,
        "requested_by": requested_by,
        "routing": get_routing_global_state(expire_ttl=False),
        "selection_revision": selection_revision,
        "pending_revision": int(pending["revision"]),
        "server": validation["server"],
    }


@xray_writer_guarded
def clear_global_fixed_server(
    *,
    requested_by: str = "admin",
    job_id: str | None = None,
) -> dict[str, Any]:
    """Return global server selection to auto/vpn-auto desired state."""

    ensure_routing_global_state()

    with db_session() as connection:
        previous = connection.execute(
            "SELECT server_mode, desired_fixed_server_id FROM routing_global_state WHERE id = 1"
        ).fetchone()
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                server_mode = 'auto',
                desired_fixed_server_id = NULL,
                applied_fixed_server_id = NULL,
                fixed_server_until = NULL,
                apply_state = 'pending',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )
        previous_mode = str(previous["server_mode"] or "auto") if previous else "auto"
        previous_server_id = str(previous["desired_fixed_server_id"] or "") if previous else ""
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision
        current_revision = read_selection_revision(connection)
        selection_revision = advance_selection_revision(connection, expected_revision=current_revision)
        if selection_revision is None:
            raise RuntimeError("Global auto intent changed while its selection fence was being reserved.")
        from fwrouter_api.services.xray_vpn_auto_pending import mark_xray_vpn_auto_pending
        mark_xray_vpn_auto_pending(
            connection,
            trigger="global_fixed_server_cleared",
            immediate=True,
            supersede_fixed_workflow=True,
        )
        if previous_mode == "fixed" or previous_server_id:
            from fwrouter_api.services.event_contract import current_event_context
            from fwrouter_api.services.events import create_event_context, write_audit_event

            entity_id = "global"
            write_audit_event(
                actor=requested_by,
                actor_attribution="caller_supplied",
                source="routing_admin_api",
                action="global_fixed_server_changed",
                event_code="routing.global_fixed_server_changed",
                legacy_event_type="routing.global_fixed_server_changed",
                entity_type="routing",
                entity_id=entity_id,
                previous_value={
                    "server_mode": previous_mode,
                    "server_ref": _audit_server_ref(previous_server_id),
                    "server_label": _event_server_label(connection, previous_server_id),
                },
                new_value={"server_mode": "auto", "server_ref": None, "server_label": None},
                context=create_event_context(
                    request_id=current_event_context().get("request_id"),
                    job_id=job_id,
                    entity_id=entity_id,
                ),
                details={"outcome": "intent_committed"},
                connection=connection,
            )

    return {
        "ok": True,
        "requested_by": requested_by,
        "routing": get_routing_global_state(expire_ttl=False),
        "selection_revision": selection_revision,
    }


@xray_writer_guarded
def _restore_global_routing_state(
    previous_state: dict[str, Any],
    *,
    expected_revision: int,
    expected_server_id: str | None,
    expected_fixed_until: str | None,
    expected_server_mode: str = "fixed",
    expected_apply_states: tuple[str, ...] = ("pending", "applying"),
) -> dict[str, Any]:
    """CAS-restore only the fixed operation whose intent still owns the row."""

    ensure_routing_global_state()

    with db_session() as connection:
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision

        if read_selection_revision(connection) != expected_revision:
            return {"ok": False, "error_code": "GLOBAL_FIXED_SERVER_STALE", "routing": get_routing_global_state(expire_ttl=False)}
        apply_placeholders = ",".join("?" for _ in expected_apply_states)
        changed = connection.execute(
            f"""
            UPDATE routing_global_state
            SET
                desired_mode = ?,
                applied_mode = ?,
                selective_default = ?,
                server_mode = ?,
                desired_fixed_server_id = ?,
                applied_fixed_server_id = ?,
                fixed_server_until = ?,
                apply_state = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
              AND server_mode = ?
              AND desired_fixed_server_id IS ?
              AND fixed_server_until IS ?
              AND apply_state IN ({apply_placeholders})
            """,
            (
                previous_state["desired_mode"],
                previous_state["applied_mode"],
                previous_state["selective_default"],
                previous_state["server_mode"],
                previous_state["desired_fixed_server_id"],
                previous_state["applied_fixed_server_id"],
                previous_state.get("fixed_server_until"),
                previous_state["apply_state"],
                previous_state["error_code"],
                previous_state["error_message"],
                expected_server_mode,
                expected_server_id,
                expected_fixed_until,
                *expected_apply_states,
            ),
        )
        if changed.rowcount != 1:
            return {"ok": False, "error_code": "GLOBAL_FIXED_SERVER_STALE", "routing": get_routing_global_state(expire_ttl=False)}
        if advance_selection_revision(connection, expected_revision=expected_revision) is None:
            raise RuntimeError("Selection revision changed while restoring fixed-server intent.")

    restored = get_routing_global_state(expire_ttl=False)
    if restored is None:
        raise RuntimeError("Failed to restore routing_global_state")
    return {"ok": True, "routing": restored}


def _fixed_selection_proof_diagnostic(proof: Any) -> dict[str, Any] | None:
    """Return only bounded, non-identifying proof facts for stale diagnostics."""

    if not isinstance(proof, dict):
        return None
    diagnostic: dict[str, Any] = {}
    for key in (
        "ok",
        "config_membership_ok",
        "native_membership_ok",
        "logical_group_native_membership_ok",
        "selected_fixed_target",
    ):
        value = proof.get(key)
        diagnostic[key] = value if type(value) is bool else None
    reason = proof.get("reason")
    diagnostic["reason"] = reason if isinstance(reason, str) and reason in {
        "fixed_target_native_membership_unverified",
        "fixed_target_config_membership_mismatch",
        "fixed_target_mapping_unresolved",
        "fixed_target_missing",
        "active_config_unavailable",
        "mihomo_health_unavailable",
    } else None
    current = proof.get("vpn_global_now")
    target = proof.get("fixed_runtime_target")
    diagnostic["vpn_global_matches_target"] = (
        current == target if isinstance(current, str) and isinstance(target, str) else None
    )
    return diagnostic


def apply_global_fixed_server(
    server_id: str,
    *,
    requested_by: str = "admin",
    management_context: dict[str, Any] | None = None,
    job_id: str | None = None,
    timeout_ms: int = 10000,
    post_check: bool = True,
    _resume_workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pre-check, persist and apply admin global fixed server.

    Admin global fixed server is selected from full active inventory and is not
    limited by vpn_auto/global_list. Runtime apply switches Mihomo vpn-global,
    not vpn-auto.

    Partial effects remain under the durable pending workflow for owned retry;
    success is published only after fresh Core, Mihomo, and Xray readback.
    """

    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoApplyResult
    from fwrouter_api.services.management_attribution import (
        build_incomplete_attribution_error,
        build_management_attribution,
    )
    from fwrouter_api.services.server_ping import check_server_delay

    if management_context is None and isinstance(_resume_workflow, dict):
        saved_context = _resume_workflow.get("management_context")
        management_context = saved_context if isinstance(saved_context, dict) else None

    attribution = build_management_attribution(
        requested_by=requested_by,
        context=management_context,
        default_requested_by="admin",
    )
    attribution_error = build_incomplete_attribution_error(attribution)
    if attribution_error is not None:
        return {
            "ok": False,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "server_id": server_id,
            "active_before": None,
            "routing_before": None,
            "routing_after": None,
            "server": None,
            "pre_check": None,
            "apply_result": None,
            "post_check": None,
            "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": attribution_error["code"],
            "error_message": attribution_error["message"],
            "error": attribution_error,
        }

    previous_state = ensure_routing_global_state()
    active_before = DEFAULT_MIHOMO_ADAPTER.get_active_server_id()

    validation = _validate_global_fixed_server(server_id)
    if not validation["ok"]:
        return {
            "ok": False,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "server_id": server_id,
            "active_before": active_before,
            "routing_before": previous_state,
            "routing_after": previous_state,
            "server": validation["server"],
            "pre_check": None,
            "apply_result": None,
            "post_check": None,
            "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
        }

    pre_check = None

    with xray_writer_guard(timeout_seconds=30.0):
        previous_state = get_routing_global_state(expire_ttl=False) or previous_state
        active_before = DEFAULT_MIHOMO_ADAPTER.get_active_server_id()
        if _resume_workflow is None:
            desired = set_global_fixed_server(
                server_id, requested_by=requested_by, job_id=job_id,
                management_context=management_context,
            )
            token = (
                _capture_fixed_apply_context(desired["routing"])
                if desired.get("ok") and isinstance(desired.get("routing"), dict)
                else None
            )
        else:
            desired = {
                "ok": True,
                "routing": previous_state,
                "selection_revision": _resume_workflow.get("selection_revision"),
                "pending_revision": _resume_workflow.get("pending_revision"),
            }
            current_workflow = _fixed_workflow_current(int(_resume_workflow.get("pending_revision") or -1))
            token = _capture_fixed_apply_context(previous_state)
            if (
                current_workflow is None
                or current_workflow.get("server_id") != server_id
                or current_workflow.get("fixed_until") != previous_state.get("fixed_server_until")
                or current_workflow.get("selection_revision") != desired["selection_revision"]
                or token is None
                or token.get("selection_revision") != desired["selection_revision"]
                or token.get("pending_revision") != current_workflow.get("pending_revision")
                or token.get("core_revision") != current_workflow.get("core_revision")
                or tuple(token.get("core_snapshot") or ()) != tuple(current_workflow.get("core_snapshot") or ())
            ):
                desired["ok"] = False
                desired["error_code"] = "GLOBAL_FIXED_SERVER_STALE"
                desired["error_message"] = "Persisted fixed workflow fences no longer match current intent."

    if not desired["ok"]:
        return {
            "ok": False,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "server_id": server_id,
            "active_before": active_before,
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"],
            "pre_check": pre_check,
            "apply_result": None,
            "post_check": None,
            "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": desired["error_code"],
            "error_message": desired["error_message"],
        }

    if token is None:
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution, "server_id": server_id,
            "active_before": active_before, "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": None, "post_check": None, "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_FENCE_UNAVAILABLE",
            "error_message": "Fixed routing intent could not be bound to the current runtime context.",
        }

    phase_at_entry = str(token.get("fixed_workflow_phase") or "intent")
    fixed_generation_already_verified = phase_at_entry in {
        "mihomo_generation_verified", "mihomo_verified", "core_verified", "xray_verified",
    }
    fixed_stage_already_committed = phase_at_entry in {"mihomo_verified", "core_verified", "xray_verified"}

    pending_revision = int(token.get("pending_revision") or 0)
    workflow = _fixed_workflow_current(pending_revision)
    if workflow is None or workflow.get("server_id") != server_id:
        return {
            "ok": False,
            "requested_by": requested_by,
            "server_id": server_id,
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "rolled_back": False,
            "error_code": "GLOBAL_FIXED_SERVER_WORKFLOW_STALE",
            "error_message": "Durable fixed workflow marker is missing or superseded.",
        }
    xray_selection_reentry = bool(
        phase_at_entry == "xray_verified"
        and workflow.get("xray_generation_operation_id")
    )

    materialized = _materialize_fixed_server_generation(
        desired["routing"], token=token, job_id=job_id or "global-fixed-server",
    )
    generation_fence = materialized.get("reentry_generation_fence")
    before_revision = token.get("source_selection_revision", token.get("selection_revision"))
    after_revision = generation_fence.get("selection_revision") if isinstance(generation_fence, dict) else None
    unchanged_config = bool(generation_fence.get("unchanged_config")) if isinstance(generation_fence, dict) else False
    active_hash = generation_fence.get("active_config_hash") if isinstance(generation_fence, dict) else None
    receipt_valid = bool(
        isinstance(generation_fence, dict)
        and materialized.get("ok") is True
        and isinstance(materialized.get("operation_id"), str)
        and materialized.get("operation_id")
        and generation_fence.get("schema") == "mihomo-reentry-generation-fence/v1"
        and generation_fence.get("operation_id") == materialized.get("operation_id")
        and generation_fence.get("job_id") == materialized.get("job_id")
        and type(generation_fence.get("before_revision")) is int
        and generation_fence.get("before_revision") == before_revision
        and type(after_revision) is int
        and after_revision == (before_revision if unchanged_config else before_revision + 1)
        and isinstance(generation_fence.get("runtime_incarnation"), str)
        and generation_fence.get("runtime_incarnation")
        and isinstance(active_hash, str)
        and len(active_hash) == 64
        and all(char in "0123456789abcdef" for char in active_hash)
        and generation_fence.get("native_proof_verified") is True
        and generation_fence.get("publication_verified") is True
        and (
            not unchanged_config
            or (
                generation_fence.get("runtime_incarnation") == token.get("runtime_incarnation")
                and active_hash == token.get("active_config_hash")
            )
        )
    )
    if not receipt_valid:
        materialized = {**materialized, "ok": False, "error_code": "MIHOMO_GENERATION_RECEIPT_INVALID"}
    else:
        if str(token.get("fixed_workflow_phase") or "") not in {"mihomo_verified", "core_verified", "xray_verified"}:
            token.update({
                "selection_revision": after_revision,
                "runtime_incarnation": generation_fence["runtime_incarnation"],
                "active_config_hash": active_hash,
            })
    if materialized.get("stage") == "fixed_intent_revalidation":
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution, "server_id": server_id,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": None, "mihomo_reconcile": materialized,
            "post_check": None, "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_STALE",
            "error_message": materialized.get("error_message"),
        }
    if not materialized.get("ok"):
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution, "server_id": server_id,
            "active_before": active_before, "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": None, "mihomo_reconcile": materialized,
            "post_check": None, "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "MIHOMO_RECONCILE_FAILED",
            "error_message": "Fixed target could not be materialized and verified in the managed runtime.",
        }

    from fwrouter_api.services.mihomo_config_status import mihomo_runtime_fixed_target_materialized

    materialized_proof = mihomo_runtime_fixed_target_materialized(desired["routing"])
    mihomo_target = str(materialized_proof.get("fixed_runtime_target") or "")
    if not materialized_proof.get("ok") or not mihomo_target:
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution, "server_id": server_id,
            "active_before": active_before, "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": None, "mihomo_reconcile": materialized,
            "fixed_materialization_proof": materialized_proof,
            "post_check": None, "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_MATERIALIZATION_UNVERIFIED",
            "error_message": "Fixed target membership is not proven by the current managed runtime.",
        }

    # Probe only after the fixed proxy has been reconciled into the active
    # Mihomo config; this supports full-inventory fixed targets that are not
    # members of the ordinary auto/global preference pools.
    pre_check = (
        {"ok": True, "skipped": True, "reason": "fixed_workflow_resume_after_mihomo_commit"}
        if fixed_generation_already_verified else check_server_delay(
            server_id,
            update_state=True,
            checked_by=f"global_fixed_pre_check:{requested_by}",
            timeout_ms=timeout_ms,
        )
    )
    if not pre_check.get("ok"):
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution, "server_id": server_id,
            "active_before": active_before,
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": None, "post_check": None, "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_PRE_CHECK_FAILED",
            "error_message": pre_check.get("error_message") or "Global fixed server pre-check failed.",
        }

    fixed_selection_stage: str | None = None
    fixed_selection_proof: dict[str, Any] | None = materialized_proof
    with xray_writer_guard(timeout_seconds=30.0):
        if not _fixed_apply_token_matches(server_id=server_id, fixed_until=str(desired["routing"].get("fixed_server_until") or ""), token=token):
            fixed_selection_stage = "owner_fence_before_selection"
            apply_result = None
            stale_before_native = True
            selected_proof = None
        elif fixed_stage_already_committed and not xray_selection_reentry:
            selected_proof = mihomo_runtime_fixed_target_materialized(
                get_routing_global_state(expire_ttl=False) or {}, require_selected=True,
            )
            fixed_selection_proof = selected_proof
            stale_before_native = not bool(
                selected_proof.get("ok")
                and selected_proof.get("fixed_runtime_target") == mihomo_target
            )
            if stale_before_native:
                fixed_selection_stage = "selected_proof_before_selection"
            apply_result = (
                MihomoApplyResult(
                    ok=True,
                    message="Previously committed fixed selector was re-read without a native write.",
                    active_server_id=mihomo_target,
                    details={"resumed_without_native_write": True},
                )
                if not stale_before_native else None
            )
        elif xray_selection_reentry:
            restored = _restore_fixed_selection_after_xray_generation(
                server_id=server_id,
                mihomo_target=mihomo_target,
                fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                token=token,
                workflow=workflow,
            )
            stale_before_native = restored.get("ok") is not True
            selected_proof = restored.get("proof") if isinstance(restored.get("proof"), dict) else None
            fixed_selection_proof = selected_proof
            apply_result = restored.get("apply_result")
        else:
            current_proof = mihomo_runtime_fixed_target_materialized(desired["routing"])
            if not current_proof.get("ok") or current_proof.get("fixed_runtime_target") != mihomo_target:
                fixed_selection_stage = "membership_before_selection"
                fixed_selection_proof = current_proof
                apply_result = None
                stale_before_native = True
                selected_proof = None
            else:
                selected_proof = mihomo_runtime_fixed_target_materialized(
                    desired["routing"], require_selected=True,
                )
                fixed_selection_proof = selected_proof
                if selected_proof.get("ok") and selected_proof.get("fixed_runtime_target") == mihomo_target:
                    stale_before_native = False
                    apply_result = MihomoApplyResult(
                        ok=True,
                        message="Fixed selector was already selected and was re-read without a native write.",
                        active_server_id=mihomo_target,
                        details={"resumed_without_native_write": True},
                    )
                elif (
                    selected_proof.get("native_membership_ok") is True
                    and selected_proof.get("logical_group_native_membership_ok") is True
                    and selected_proof.get("config_membership_ok") is True
                    and selected_proof.get("fixed_runtime_target") == mihomo_target
                    and selected_proof.get("vpn_global_now") != mihomo_target
                ):
                    stale_before_native = False
                    apply_result = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector("vpn-global", mihomo_target)
                    if apply_result.ok:
                        selected_proof = mihomo_runtime_fixed_target_materialized(
                            desired["routing"], require_selected=True,
                        )
                        fixed_selection_proof = selected_proof
                        if not selected_proof.get("ok"):
                            fixed_selection_stage = "selected_proof_after_selection"
                            apply_result = None
                            stale_before_native = True
                        elif not _fixed_apply_token_matches(
                            server_id=server_id,
                            fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                            token=token,
                        ):
                            fixed_selection_stage = "owner_fence_after_selection"
                            apply_result = None
                            stale_before_native = True
                    else:
                        selected_proof = None
                else:
                    apply_result = None
                    stale_before_native = True
                    fixed_selection_stage = "selected_proof_before_selection"

    if stale_before_native or apply_result is None:
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution, "server_id": server_id,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": apply_result.to_dict() if apply_result else None,
            "fixed_selection_stage": fixed_selection_stage,
            "fixed_selection_proof": _fixed_selection_proof_diagnostic(fixed_selection_proof),
            "post_check": None, "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_STALE",
            "error_message": "Fixed routing changed during runtime verification; no completion was recorded.",
        }

    if not apply_result.ok or not selected_proof.get("ok"):
        failure_code = apply_result.error_code if not apply_result.ok else "GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED"
        failure_message = apply_result.error_message or apply_result.message if not apply_result.ok else "Fixed target native selection was not verified."
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution, "server_id": server_id,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": apply_result.to_dict(),
            "fixed_materialization_proof": selected_proof,
            "post_check": None, "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": failure_code, "error_message": failure_message,
        }

    intermediate = (
        {"routing": get_routing_global_state(expire_ttl=False), "selection_revision": token.get("selection_revision")}
        if fixed_stage_already_committed
        else _commit_fixed_mihomo_stage(
            server_id=server_id,
            fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
            token=token,
        )
    )
    if intermediate is None:
        return {
            "ok": False,
            "requested_by": requested_by,
            "server_id": server_id,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"],
            "pre_check": pre_check,
            "apply_result": apply_result.to_dict(),
            "post_check": None,
            "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_STALE",
            "error_message": "Verified Mihomo selection could not publish its fenced intermediate state.",
        }

    post_check_result = None
    post_check_failed_no_rollback = False

    if post_check:
        post_check_result = check_server_delay(
            server_id,
            update_state=True,
            checked_by=f"global_fixed_post_check:{requested_by}",
            timeout_ms=timeout_ms,
        )
        post_check_failed_no_rollback = not post_check_result["ok"]

    # The optional network probe is deliberately outside the shared writer
    # lock. Revalidate both ownership and the selected native target before
    # starting the Core owner; a newer auto intent or selector drift must not
    # launch a nested Core apply on stale fixed routing.
    with xray_writer_guard(timeout_seconds=30.0):
        if not _fixed_apply_token_matches(
            server_id=server_id,
            fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
            token=token,
        ):
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution, "server_id": server_id,
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "GLOBAL_FIXED_SERVER_STALE",
                "error_message": "Fixed workflow was superseded during the unlocked post-check.",
            }
        post_probe_native_proof = mihomo_runtime_fixed_target_materialized(
            get_routing_global_state(expire_ttl=False) or {}, require_selected=True,
        )
        if not post_probe_native_proof.get("ok") or post_probe_native_proof.get("fixed_runtime_target") != mihomo_target:
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution, "server_id": server_id,
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "fixed_materialization_proof": post_probe_native_proof,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED",
                "error_message": "Fixed native selection changed during the unlocked post-check.",
            }

    apply_state = "clean"
    error_code = None
    error_message = None

    if post_check_failed_no_rollback:
        apply_state = "degraded"
        error_code = post_check_result["error_code"] if post_check_result else None
        error_message = (
            post_check_result["error_message"]
            if post_check_result
            else "Global fixed server post-check failed."
        )

    workflow = _fixed_workflow_current(pending_revision) or {}
    core_proof = _fixed_core_readback()
    core_apply_result: dict[str, Any] | None = None
    if core_proof.get("ok") is not True:
        from fwrouter_api.services.apply_orchestrator_public import apply_global_mode_immediately
        from fwrouter_api.services.core_intent import read_core_intent_revision

        with db_session() as connection:
            core_row = connection.execute(
                "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
            ).fetchone()
            current_core_revision = read_core_intent_revision(connection)
            current_core_snapshot = (
                tuple(str(core_row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
                if core_row is not None else None
            )
        if (
            current_core_snapshot is None
            or current_core_snapshot[:2] != ("enabled", "managed")
            or current_core_revision != workflow.get("core_revision")
            or current_core_snapshot != tuple(workflow.get("core_snapshot") or ())
        ):
            return {
                "ok": False, "requested_by": requested_by, "server_id": server_id,
                "routing_before": previous_state, "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "CORE_INTENT_STALE",
                "error_message": "Enabled Core intent changed during fixed routing convergence.",
                "core_native_proof": core_proof,
            }
        mode = str(get_routing_global_state(expire_ttl=False).get("desired_mode") or "").strip().lower()
        core_apply_result = apply_global_mode_immediately(
            mode,
            requested_by=f"global_fixed_server:{requested_by}",
            expected_selection_revision=int(token["selection_revision"]),
            expected_core_revision=int(workflow["core_revision"]),
            expected_core_snapshot=current_core_snapshot,
        )
        if core_apply_result.get("ok") is not True:
            return {
                "ok": False, "requested_by": requested_by, "server_id": server_id,
                "routing_before": previous_state, "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": str(core_apply_result.get("code") or core_apply_result.get("error_code") or "CORE_APPLY_FAILED"),
                "error_message": str(core_apply_result.get("message") or core_apply_result.get("error_message") or "Owned Core apply failed."),
                "core_apply": core_apply_result,
            }
        core_proof = _fixed_core_readback()
        actual_core_apply_id = str(core_apply_result.get("apply_id") or "")
        if core_proof.get("ok") is not True or (
            actual_core_apply_id and core_proof.get("apply_id") != actual_core_apply_id
        ):
            return {
                "ok": False, "requested_by": requested_by, "server_id": server_id,
                "routing_before": previous_state, "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "CORE_NATIVE_PROOF_UNVERIFIED",
                "error_message": "Fresh Core proof did not match the owned fixed-workflow apply.",
                "core_apply": core_apply_result, "core_native_proof": core_proof,
            }
    with db_session() as connection:
        core_row = connection.execute(
            "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
        ).fetchone()
    core_snapshot_after = tuple(
        str(core_row[key]) for key in ("desired_state", "lifecycle_mode", "apply_state")
    ) if core_row is not None else ()
    workflow = _update_fixed_workflow(
        pending_revision,
        phase="core_verified",
        core_snapshot=list(core_snapshot_after),
        core_apply_id=str(core_proof.get("apply_id") or ""),
        core_manifest_hash=str(core_proof.get("manifest_hash") or ""),
        core_enforcement_level=str(core_proof.get("enforcement_level") or ""),
        core_live_mode=str(core_proof.get("live_global_mode") or ""),
    )
    if workflow is None:
        return {
            "ok": False, "requested_by": requested_by, "server_id": server_id,
            "routing_before": previous_state, "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": apply_result.to_dict(), "post_check": post_check_result,
            "rolled_back": False, "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_STALE",
            "error_message": "Durable fixed workflow changed before Core proof publication.",
        }
    token["core_snapshot"] = core_snapshot_after
    if not _refresh_fixed_token_after_core_apply(
        server_id=server_id,
        fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
        token=token,
    ):
        return {
            "ok": False, "requested_by": requested_by, "server_id": server_id,
            "routing_before": previous_state, "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": apply_result.to_dict(), "post_check": post_check_result,
            "rolled_back": False, "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_STALE",
            "error_message": "Owned Core completion could not be rebound to the existing fixed fences.",
        }

    xray_proof = _fixed_xray_readback()
    xray_apply_result: dict[str, Any] | None = None
    if xray_proof.get("state") == "drift":
        from fwrouter_api.services.xray_subscription_service import reconcile_xray_vpn_auto_subscription

        xray_apply_result = reconcile_xray_vpn_auto_subscription(
            requested_by=f"global_fixed_server:{requested_by}",
            _fixed_workflow_revision=pending_revision,
            verification_callback=_fixed_xray_generation_callback(
                server_id=server_id,
                mihomo_target=mihomo_target,
                fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                token=token,
            ),
        )
        if (
            xray_apply_result.get("ok") is not True
            or str(xray_apply_result.get("status") or "") != "success"
            or not _fixed_xray_generation_receipt_matches(
                xray_apply_result, server_id=server_id, token=token,
            )
        ):
            return {
                "ok": False, "requested_by": requested_by, "server_id": server_id,
                "routing_before": previous_state, "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "XRAY_RECONCILE_FAILED",
                "error_message": "Owned Xray reconciliation did not complete successfully.",
                "xray_apply": xray_apply_result,
            }
        xray_proof = _fixed_xray_readback()
    if xray_proof.get("state") not in {"verified", "not_applicable"}:
        return {
            "ok": False, "requested_by": requested_by, "server_id": server_id,
            "routing_before": previous_state, "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": apply_result.to_dict(), "post_check": post_check_result,
            "rolled_back": False, "post_check_failed_no_rollback": False,
            "error_code": "XRAY_NATIVE_PROOF_UNVERIFIED",
            "error_message": "Current Xray source, artifact, active config, or runtime could not be proven.",
            "xray_native_proof": xray_proof,
        }
    workflow = _update_fixed_workflow(
        pending_revision,
        phase="xray_verified",
        xray_state=str(xray_proof.get("state")),
        xray_runtime_incarnation=xray_proof.get("runtime_incarnation"),
        xray_config_sha256=xray_proof.get("active_config_sha256"),
        xray_artifact_sha256=xray_proof.get("artifact_sha256"),
    )
    if workflow is None:
        return {
            "ok": False, "requested_by": requested_by, "server_id": server_id,
            "routing_before": previous_state, "routing_after": get_routing_global_state(expire_ttl=False),
            "server": validation["server"], "pre_check": pre_check,
            "apply_result": apply_result.to_dict(), "post_check": post_check_result,
            "rolled_back": False, "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_STALE",
            "error_message": "Durable fixed workflow changed before Xray proof publication.",
        }

    if xray_apply_result is not None and workflow.get("xray_generation_operation_id"):
        selection_restore = _restore_fixed_selection_after_xray_generation(
            server_id=server_id,
            mihomo_target=mihomo_target,
            fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
            token=token,
            workflow=workflow,
        )
        if selection_restore.get("ok") is not True:
            return {
                "ok": False, "requested_by": requested_by, "server_id": server_id,
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "fixed_materialization_proof": selection_restore.get("proof"),
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": str(selection_restore.get("error_code") or "GLOBAL_FIXED_SERVER_STALE"),
                "error_message": "Fixed selection could not be rebound after the owned Xray generation.",
                "xray_native_proof": xray_proof,
            }

    generation_reservation = workflow.get("mihomo_generation_reservation")

    # Re-read every owner proof while holding the shared writer fence, then
    # clear the durable workflow marker in the same transaction that records
    # completion. Intermediate route state is already truthful and clean; the
    # pending workflow marker remains the authority until this CAS succeeds.
    with xray_writer_guard(timeout_seconds=30.0):
        final_native_proof = mihomo_runtime_fixed_target_materialized(
            get_routing_global_state(expire_ttl=False) or {}, require_selected=True,
        )
        final_core_proof = _fixed_core_readback()
        final_xray_proof = _fixed_xray_readback()
        if (
            not final_native_proof.get("ok")
            or final_native_proof.get("fixed_runtime_target") != mihomo_target
            or final_core_proof.get("ok") is not True
            or final_core_proof.get("apply_id") != workflow.get("core_apply_id")
            or final_core_proof.get("manifest_hash") != workflow.get("core_manifest_hash")
            or final_core_proof.get("enforcement_level") != workflow.get("core_enforcement_level")
            or final_core_proof.get("live_global_mode") != workflow.get("core_live_mode")
            or final_xray_proof.get("state") != workflow.get("xray_state")
            or final_xray_proof.get("runtime_incarnation") != workflow.get("xray_runtime_incarnation")
            or final_xray_proof.get("active_config_sha256") != workflow.get("xray_config_sha256")
            or final_xray_proof.get("artifact_sha256") != workflow.get("xray_artifact_sha256")
            or not _fixed_apply_token_matches(
                server_id=server_id,
                fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                token=token,
            )
        ):
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution, "server_id": server_id,
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "fixed_materialization_proof": final_native_proof,
                "core_native_proof": final_core_proof,
                "xray_native_proof": final_xray_proof,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "GLOBAL_FIXED_SERVER_FINAL_PROOF_FAILED",
                "error_message": "A required native owner proof changed before fixed workflow completion.",
            }

        from fwrouter_api.services.core_intent import read_core_intent_revision
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
        from fwrouter_api.services.xray_vpn_auto_pending import (
            _complete_revision_in_connection, _load_row,
        )

        with db_session() as connection:
            route = connection.execute(
                """SELECT server_mode, desired_fixed_server_id, applied_fixed_server_id,
                          fixed_server_until, apply_state, error_code, error_message,
                          datetime(fixed_server_until) IS NOT NULL
                            AND datetime(fixed_server_until) > CURRENT_TIMESTAMP AS ttl_live
                   FROM routing_global_state WHERE id=1""",
            ).fetchone()
            core = connection.execute(
                "SELECT desired_state, lifecycle_mode, apply_state FROM modules WHERE module_name='core'",
            ).fetchone()
            pending = _load_row(connection)
            saved_workflow = pending.get("workflow") if isinstance(pending, dict) else None
            if (
                route is None
                or (str(route["server_mode"] or "") != "fixed")
                or route["desired_fixed_server_id"] != server_id
                or route["applied_fixed_server_id"] != server_id
                or route["fixed_server_until"] != desired["routing"].get("fixed_server_until")
                or str(route["apply_state"] or "") != "clean"
                or route["error_code"] is not None or route["error_message"] is not None
                or not bool(route["ttl_live"])
                or read_selection_revision(connection) != token.get("selection_revision")
                or read_core_intent_revision(connection) != token.get("core_revision")
                or core is None
                or tuple(str(core[key]) for key in ("desired_state", "lifecycle_mode", "apply_state"))
                    != tuple(token.get("core_snapshot") or ())
                or tuple(token.get("core_snapshot") or ())[:2] != ("enabled", "managed")
                or not isinstance(pending, dict) or pending.get("pending") is not True
                or pending.get("revision") != pending_revision
                or not isinstance(saved_workflow, dict)
                or saved_workflow.get("kind") != "global_fixed_server"
                or saved_workflow.get("phase") != "xray_verified"
                or saved_workflow.get("server_id") != server_id
                or saved_workflow.get("fixed_until") != desired["routing"].get("fixed_server_until")
                or saved_workflow.get("selection_revision") != token.get("selection_revision")
                or saved_workflow.get("core_revision") != token.get("core_revision")
                or saved_workflow.get("core_snapshot") != list(token.get("core_snapshot") or ())
                or saved_workflow.get("core_apply_id") != final_core_proof.get("apply_id")
                or saved_workflow.get("core_manifest_hash") != final_core_proof.get("manifest_hash")
                or saved_workflow.get("xray_state") != final_xray_proof.get("state")
                or saved_workflow.get("xray_config_sha256") != final_xray_proof.get("active_config_sha256")
                or saved_workflow.get("xray_runtime_incarnation") != final_xray_proof.get("runtime_incarnation")
                or saved_workflow.get("xray_artifact_sha256") != final_xray_proof.get("artifact_sha256")
                or not _complete_revision_in_connection(connection, pending_revision)
            ):
                connection.rollback()
                return {
                    "ok": False, "requested_by": requested_by,
                    "management_attribution": attribution, "server_id": server_id,
                    "routing_before": previous_state,
                    "routing_after": get_routing_global_state(expire_ttl=False),
                    "server": validation["server"], "pre_check": pre_check,
                    "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                    "rolled_back": False, "post_check_failed_no_rollback": False,
                    "error_code": "GLOBAL_FIXED_SERVER_STALE",
                    "error_message": "Fixed workflow lost an owner fence before atomic completion.",
                }
            changed = connection.execute(
                """UPDATE routing_global_state SET apply_state=?, error_code=?,
                          error_message=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=1 AND server_mode='fixed' AND desired_fixed_server_id=?
                     AND applied_fixed_server_id=? AND fixed_server_until=? AND apply_state='clean'""",
                (
                    apply_state, error_code, error_message, server_id, server_id,
                    desired["routing"].get("fixed_server_until"),
                ),
            )
            if changed.rowcount != 1:
                connection.rollback()
                return {
                    "ok": False, "requested_by": requested_by, "server_id": server_id,
                    "routing_before": previous_state,
                    "routing_after": get_routing_global_state(expire_ttl=False),
                    "error_code": "GLOBAL_FIXED_SERVER_STALE",
                    "error_message": "Fixed routing state changed before its completion update.",
                }

    checkpoint_cleanup: dict[str, bool] | None = None
    if isinstance(generation_reservation, dict):
        from fwrouter_api.services.mihomo_reconcile import _cleanup_fixed_generation_checkpoint

        removed = _cleanup_fixed_generation_checkpoint(
            str(generation_reservation.get("operation_id") or ""),
            generation_reservation.get("checkpoint_ref"),
        )
        checkpoint_cleanup = {"ok": removed, "retained": not removed}

    routing_after = get_routing_global_state()

    from fwrouter_api.services.logs import write_operational_log

    write_operational_log(
        event_type="global_fixed_server_applied",
        message="Global fixed server was applied.",
        details={
            "requested_by": requested_by,
            "management_attribution": attribution,
            "server_id": server_id,
            "mihomo_target": mihomo_target,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "fixed_server_until": routing_after.get("fixed_server_until") if routing_after else None,
            "post_check_failed_no_rollback": post_check_failed_no_rollback,
        },
    )

    return {
        "ok": True,
        "requested_by": requested_by,
        "management_attribution": attribution,
        "server_id": server_id,
        "active_before": active_before,
        "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
        "routing_before": previous_state,
        "routing_after": routing_after,
        "server": validation["server"],
        "pre_check": pre_check,
        "apply_result": apply_result.to_dict(),
        "mihomo_target": mihomo_target,
        "post_check": post_check_result,
        "rolled_back": False,
        "post_check_failed_no_rollback": post_check_failed_no_rollback,
        "error_code": error_code,
        "error_message": error_message,
        "unified_fixed_proof": {
            "pending_revision": pending_revision,
            "selection_revision": token["selection_revision"],
            "core_apply_id": final_core_proof.get("apply_id"),
            "core_manifest_hash": final_core_proof.get("manifest_hash"),
            "core_enforcement_level": final_core_proof.get("enforcement_level"),
            "mihomo_target": mihomo_target,
            "mihomo_config_sha256": token.get("active_config_hash"),
            "mihomo_runtime_incarnation": token.get("runtime_incarnation"),
            "xray_state": final_xray_proof.get("state"),
            "xray_config_sha256": final_xray_proof.get("active_config_sha256"),
            "xray_artifact_sha256": final_xray_proof.get("artifact_sha256"),
            "xray_runtime_incarnation": final_xray_proof.get("runtime_incarnation"),
        },
        "checkpoint_cleanup": checkpoint_cleanup,
    }


def _global_fixed_server_error_stage(result: dict[str, Any]) -> str:
    if result.get("stage"):
        return str(result["stage"])
    if result.get("error_code") in {"SERVER_NOT_FOUND_OR_INACTIVE", "SERVER_MANUALLY_DELETED"}:
        return "validation"
    if result.get("error_code") == "GLOBAL_FIXED_SERVER_PRE_CHECK_FAILED":
        return "pre_check"
    if result.get("error_code") == "GLOBAL_FIXED_SERVER_APPLY_FAILED":
        return "apply"
    if result.get("post_check_failed_no_rollback"):
        return "post_check"
    if result.get("error_code"):
        return "apply"
    return "verified" if result.get("ok") else "unknown"


def run_global_fixed_server_apply_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run a global fixed-server apply from the existing job framework."""

    input_data = job.get("input") or {}
    server_id = str(input_data.get("server_id") or "").strip()
    requested_by = str(input_data.get("requested_by") or job.get("requested_by") or "admin")
    result = apply_global_fixed_server(
        server_id,
        requested_by=requested_by,
        management_context=input_data.get("management_context") if isinstance(input_data.get("management_context"), dict) else None,
        job_id=str(job["job_id"]),
        timeout_ms=int(input_data.get("timeout_ms") or 10000),
        post_check=bool(input_data.get("post_check", True)),
    )
    stage = _global_fixed_server_error_stage(result)
    error_code = result.get("error_code") or ("GLOBAL_FIXED_SERVER_APPLY_FAILED" if not result.get("ok") else None)
    error_message = (
        result.get("error_message")
        or result.get("message")
        or ("Global fixed server apply failed." if not result.get("ok") else None)
    )
    payload = {
        "handler": "global_fixed_server_apply",
        "job_id": job["job_id"],
        "job_type": job["job_type"],
        "ok": bool(result.get("ok")),
        "status": "success" if result.get("ok") else "failed",
        "stage": stage,
        "server_id": server_id,
        "requested_by": requested_by,
        "message": (
            "Global fixed server apply completed."
            if result.get("ok")
            else str(error_message or "Global fixed server apply failed.")
        ),
        "error_code": error_code,
        "error_message": error_message,
        "global_fixed_server": {
            **result,
            "stage": stage,
            "job_id": job["job_id"],
            "server_id": server_id,
            "error_code": error_code,
            "error_message": error_message,
        },
    }
    if not result.get("ok"):
        payload["job_status"] = "failed"
    return payload


def resume_global_fixed_server_workflow(
    workflow: dict[str, Any],
    *,
    requested_by: str = "fixed_server_recovery",
) -> dict[str, Any]:
    """Resume the existing fixed intent without allocating a new revision."""
    if not isinstance(workflow, dict) or workflow.get("kind") != "global_fixed_server":
        return {"ok": False, "status": "blocked", "error_code": "GLOBAL_FIXED_WORKFLOW_INVALID"}
    server_id = str(workflow.get("server_id") or "").strip()
    if not server_id or type(workflow.get("pending_revision")) is not int:
        return {"ok": False, "status": "blocked", "error_code": "GLOBAL_FIXED_WORKFLOW_INVALID"}
    context = workflow.get("management_context")
    return apply_global_fixed_server(
        server_id,
        requested_by=str(workflow.get("requested_by") or requested_by),
        management_context=context if isinstance(context, dict) else None,
        job_id=str(workflow.get("owner_job_id") or "global-fixed-server-recovery"),
        _resume_workflow=workflow,
    )


def submit_global_fixed_server_apply_job(
    server_id: str,
    *,
    requested_by: str = "admin",
    management_context: dict[str, Any] | None = None,
    timeout_ms: int = 10000,
    post_check: bool = True,
) -> dict[str, Any]:
    """Create and start a global fixed-server apply job without waiting for completion."""

    from fwrouter_api.jobs.manager import get_default_job_manager

    manager = get_default_job_manager()
    job = manager.create(
        "global_fixed_server_apply",
        lock_key="global_fixed_server",
        requested_by=requested_by,
        input_data={
            "server_id": server_id,
            "requested_by": requested_by,
            "management_context": management_context or {},
            "timeout_ms": timeout_ms,
            "post_check": post_check,
        },
    )
    return manager.start_job(job["job_id"]) or job


@xray_writer_guarded
def _global_auto_intent_matches(token: dict[str, Any], *, expected_revision: int | None = None) -> bool:
    from fwrouter_api.services.core_intent import (
        assert_core_intent_snapshot_current,
        read_core_runtime_context,
    )
    from fwrouter_api.services.mihomo_reconcile import current_mihomo_input_fingerprint
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    try:
        with db_session() as connection:
            row = connection.execute(
                """SELECT desired_mode, applied_mode, selective_default, server_mode,
                          desired_fixed_server_id, applied_fixed_server_id, fixed_server_until,
                          active_auto_server_id, apply_state, error_code, error_message, updated_at
                   FROM routing_global_state WHERE id=1"""
            ).fetchone()
            revision = read_selection_revision(connection)
            if (
                row is None
                or revision != (expected_revision if expected_revision is not None else token.get("selection_revision"))
                or not assert_core_intent_snapshot_current(
                    connection, int(token["core_revision"]), tuple(token["core_snapshot"]),
                )
            ):
                return False
            if (
                str(row["server_mode"] or "auto") != "auto"
                or row["desired_fixed_server_id"] is not None
                or row["applied_fixed_server_id"] is not None
                or row["fixed_server_until"] is not None
                or str(row["apply_state"] or "") != "pending"
                or row["error_code"] is not None
                or row["error_message"] is not None
            ):
                return False
            persisted_routing = {key: row[key] for key in (
                "desired_mode", "applied_mode", "selective_default", "server_mode",
                "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until",
                "active_auto_server_id", "apply_state", "error_code", "error_message", "updated_at",
            )}
        fingerprint = current_mihomo_input_fingerprint(persisted_routing)
        return bool(
            isinstance(fingerprint, dict)
            and fingerprint.get("hash") == token.get("input_hash")
            and read_core_runtime_context() == token.get("core_runtime_context")
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _global_auto_materialization_callback(token: dict[str, Any]):
    def verify(*, operation_id: str, expected_selection_revision: int) -> dict[str, Any]:
        owned_revision = int(token.get("selection_revision", -1))
        revision_is_reconcile_owned = expected_selection_revision in {owned_revision, owned_revision + 1}
        current = revision_is_reconcile_owned and _global_auto_intent_matches(
            token, expected_revision=expected_selection_revision,
        )
        return {
            "ok": bool(current),
            "operation_id": operation_id,
            "selection_revision": expected_selection_revision,
            "selected_fixed_target": False,
        }

    return verify


def _global_auto_apply_token_matches(token: dict[str, Any]) -> bool:
    from fwrouter_api.services.mihomo_config_paths import _resolved_base_config_path
    from fwrouter_api.services.mihomo_reconcile import _file_hash
    from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation

    return bool(
        _global_auto_intent_matches(token)
        and get_mihomo_runtime_incarnation(timeout_seconds=5.0) == token.get("runtime_incarnation")
        and _file_hash(_resolved_base_config_path()) == token.get("active_config_hash")
    )


@xray_writer_guarded
def apply_global_auto_server(
    *,
    requested_by: str = "admin",
    management_context: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Return global egress selector to vpn-auto.

    This clears admin global fixed server and switches Mihomo vpn-global back to
    vpn-auto. It does not run selector itself.
    """

    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
    from fwrouter_api.services.management_attribution import (
        build_incomplete_attribution_error,
        build_management_attribution,
    )

    attribution = build_management_attribution(
        requested_by=requested_by,
        context=management_context,
        default_requested_by="admin",
    )
    attribution_error = build_incomplete_attribution_error(attribution)
    if attribution_error is not None:
        return {
            "ok": False,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "active_before": None,
            "active_after": None,
            "routing_before": None,
            "routing_after": None,
            "desired_result": None,
            "apply_result": None,
            "rolled_back": False,
            "error_code": attribution_error["code"],
            "error_message": attribution_error["message"],
            "error": attribution_error,
        }

    previous_state = ensure_routing_global_state()
    active_before = DEFAULT_MIHOMO_ADAPTER.get_active_server_id()

    desired = clear_global_fixed_server(requested_by=requested_by, job_id=job_id)
    if not desired.get("ok") or not isinstance(desired.get("routing"), dict):
        return {"ok": False, "error_code": "GLOBAL_AUTO_INTENT_FAILED", "desired_result": desired}

    token = _capture_global_auto_apply_context(desired["routing"])
    if token is None:
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "desired_result": desired,
            "apply_result": None,
            "rolled_back": False,
            "error_code": "GLOBAL_AUTO_FENCE_UNAVAILABLE",
            "error_message": "Auto routing intent could not be bound to current runtime context.",
        }

    from fwrouter_api.services.mihomo_reconcile import reconcile_mihomo_runtime

    materialized = reconcile_mihomo_runtime(
        routing=desired["routing"], job_id=job_id or "global-auto-server",
        verification_callback=_global_auto_materialization_callback(token),
    )
    receipt = materialized.get("reentry_generation_fence")
    before_revision = token.get("selection_revision")
    unchanged_config = bool(receipt.get("unchanged_config")) if isinstance(receipt, dict) else False
    after_revision = receipt.get("selection_revision") if isinstance(receipt, dict) else None
    receipt_valid = bool(
        materialized.get("ok") is True
        and isinstance(materialized.get("operation_id"), str)
        and materialized.get("operation_id")
        and isinstance(receipt, dict)
        and receipt.get("schema") == "mihomo-reentry-generation-fence/v1"
        and receipt.get("operation_id") == materialized.get("operation_id")
        and receipt.get("job_id") == materialized.get("job_id")
        and type(receipt.get("before_revision")) is int
        and receipt.get("before_revision") == before_revision
        and type(after_revision) is int
        and after_revision == (before_revision if unchanged_config else before_revision + 1)
        and isinstance(receipt.get("runtime_incarnation"), str)
        and receipt.get("runtime_incarnation")
        and isinstance(receipt.get("active_config_hash"), str)
        and receipt.get("native_proof_verified") is True
        and receipt.get("publication_verified") is True
    )
    if not receipt_valid:
        return {
            "ok": False, "requested_by": requested_by,
            "management_attribution": attribution,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "desired_result": desired,
            "mihomo_reconcile": materialized,
            "apply_result": None,
            "rolled_back": False,
            "error_code": "MIHOMO_RECONCILE_FAILED",
            "error_message": "Auto selector membership could not be materialized and verified.",
        }
    token.update({
        "selection_revision": after_revision,
        "runtime_incarnation": receipt["runtime_incarnation"],
        "active_config_hash": receipt["active_config_hash"],
    })

    apply_result = None
    final_proof: dict[str, Any] | None = None
    with xray_writer_guard(timeout_seconds=30.0):
        if _global_auto_apply_token_matches(token):
            apply_result = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector("vpn-global", "vpn-auto")
            if apply_result.ok:
                from fwrouter_api.services.mihomo_config_status import mihomo_runtime_satisfies_routing

                final_proof = mihomo_runtime_satisfies_routing(desired["routing"])
                if not final_proof.get("ok") or not _global_auto_apply_token_matches(token):
                    apply_result = None

    if apply_result is None or not apply_result.ok or not final_proof or not final_proof.get("ok"):
        with xray_writer_guard(timeout_seconds=30.0), db_session() as connection:
            if _global_auto_apply_token_matches(token):
                connection.execute(
                    """UPDATE routing_global_state SET apply_state='failed',
                              error_code='GLOBAL_AUTO_NATIVE_PROOF_FAILED',
                              error_message='Auto selector state could not be verified.',
                              updated_at=CURRENT_TIMESTAMP
                       WHERE id=1 AND server_mode='auto' AND desired_fixed_server_id IS NULL
                         AND fixed_server_until IS NULL AND apply_state='pending'"""
                )
        return {
            "ok": False,
            "requested_by": requested_by,
            "management_attribution": attribution,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "routing_before": previous_state,
            "routing_after": get_routing_global_state(expire_ttl=False),
            "desired_result": desired,
            "mihomo_reconcile": materialized,
            "apply_result": apply_result.to_dict() if apply_result else None,
            "fixed_materialization_proof": final_proof,
            "rolled_back": False,
            "error_code": "GLOBAL_AUTO_NATIVE_PROOF_FAILED",
            "error_message": "Auto selector state could not be verified; intent remains unclean.",
        }

    with xray_writer_guard(timeout_seconds=30.0), db_session() as connection:
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
        if not _global_auto_apply_token_matches(token):
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution,
                "active_before": active_before,
                "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "desired_result": desired,
                "apply_result": apply_result.to_dict(),
                "rolled_back": False,
                "error_code": "GLOBAL_AUTO_STALE",
                "error_message": "Auto routing intent changed before completion.",
            }
        changed = connection.execute(
            """UPDATE routing_global_state SET apply_state='clean', error_code=NULL,
                      error_message=NULL, updated_at=CURRENT_TIMESTAMP
               WHERE id=1 AND server_mode='auto' AND desired_fixed_server_id IS NULL
                 AND applied_fixed_server_id IS NULL AND fixed_server_until IS NULL
                 AND apply_state='pending' AND error_code IS NULL AND error_message IS NULL"""
        )
        if changed.rowcount != 1 or advance_selection_revision(
            connection, expected_revision=token["selection_revision"],
        ) is None:
            connection.rollback()
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution,
                "active_before": active_before,
                "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "desired_result": desired,
                "apply_result": apply_result.to_dict(),
                "rolled_back": False,
                "error_code": "GLOBAL_AUTO_STALE",
                "error_message": "Auto routing intent changed during completion.",
            }

    routing_after = get_routing_global_state()

    from fwrouter_api.services.logs import write_operational_log

    write_operational_log(
        event_type="global_fixed_server_cleared",
        message="Global fixed server was cleared and vpn-global was returned to vpn-auto.",
        details={
            "requested_by": requested_by,
            "management_attribution": attribution,
            "active_before": active_before,
            "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
            "active_auto_server_id": routing_after.get("active_auto_server_id") if routing_after else None,
        },
    )

    return {
        "ok": True,
        "requested_by": requested_by,
        "management_attribution": attribution,
        "active_before": active_before,
        "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
        "routing_before": previous_state,
        "routing_after": routing_after,
        "desired_result": desired,
        "apply_result": apply_result.to_dict(),
        "rolled_back": False,
        "error_code": None,
        "error_message": None,
    }
