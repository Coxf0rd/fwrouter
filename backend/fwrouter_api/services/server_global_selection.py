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
from fwrouter_api.adapters.xray_common import xray_writer_guard, xray_writer_guarded


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
                or str(row["apply_state"] or "") not in {"pending", "applying"}
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


def _materialize_fixed_server_generation(
    routing: dict[str, Any], *, token: dict[str, Any], job_id: str,
) -> dict[str, Any]:
    """Revalidate the captured fixed intent and serialize its generation owner."""
    from fwrouter_api.services.mihomo_reconcile import reconcile_mihomo_runtime

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
        return reconcile_mihomo_runtime(
            routing=routing,
            job_id=job_id,
            verification_callback=_fixed_materialization_callback(routing, token),
            generation_materialization_callback=_fixed_materialization_callback(routing, token),
        )


@xray_writer_guarded
def set_global_fixed_server(
    server_id: str,
    *,
    requested_by: str = "admin",
    job_id: str | None = None,
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


def apply_global_fixed_server(
    server_id: str,
    *,
    requested_by: str = "admin",
    management_context: dict[str, Any] | None = None,
    job_id: str | None = None,
    timeout_ms: int = 10000,
    post_check: bool = True,
) -> dict[str, Any]:
    """Pre-check, persist and apply admin global fixed server.

    Admin global fixed server is selected from full active inventory and is not
    limited by vpn_auto/global_list. Runtime apply switches Mihomo vpn-global,
    not vpn-auto.

    Apply failure rolls back global routing state. Post-check failure does not
    roll back the successful selector switch.
    """

    from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER
    from fwrouter_api.services.management_attribution import (
        build_incomplete_attribution_error,
        build_management_attribution,
    )
    from fwrouter_api.services.server_ping import check_server_delay

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
        desired = set_global_fixed_server(server_id, requested_by=requested_by, job_id=job_id)
        token = (
            _capture_fixed_apply_context(desired["routing"])
            if desired.get("ok") and isinstance(desired.get("routing"), dict)
            else None
        )

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

    materialized = _materialize_fixed_server_generation(
        desired["routing"], token=token, job_id=job_id or "global-fixed-server",
    )
    generation_fence = materialized.get("reentry_generation_fence")
    before_revision = token.get("selection_revision")
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
    pre_check = check_server_delay(
        server_id,
        update_state=True,
        checked_by=f"global_fixed_pre_check:{requested_by}",
        timeout_ms=timeout_ms,
    )
    if not pre_check.get("ok"):
        with xray_writer_guard(timeout_seconds=30.0):
            if _fixed_apply_token_matches(
                server_id=server_id,
                fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                token=token,
            ):
                with db_session() as connection:
                    connection.execute(
                        """UPDATE routing_global_state SET apply_state='failed', error_code=?,
                                  error_message=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=1 AND server_mode='fixed' AND desired_fixed_server_id=?
                             AND fixed_server_until=? AND apply_state='pending'""",
                        (
                            "GLOBAL_FIXED_SERVER_PRE_CHECK_FAILED",
                            pre_check.get("error_message") or "Global fixed server pre-check failed.",
                            server_id,
                            desired["routing"].get("fixed_server_until"),
                        ),
                    )
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

    with xray_writer_guard(timeout_seconds=30.0):
        if not _fixed_apply_token_matches(server_id=server_id, fixed_until=str(desired["routing"].get("fixed_server_until") or ""), token=token):
            apply_result = None
            stale_before_native = True
        else:
            current_proof = mihomo_runtime_fixed_target_materialized(desired["routing"])
            if not current_proof.get("ok") or current_proof.get("fixed_runtime_target") != mihomo_target:
                apply_result = None
                stale_before_native = True
            else:
                stale_before_native = False
                apply_result = DEFAULT_MIHOMO_ADAPTER.apply_server_to_selector("vpn-global", mihomo_target)
                if apply_result.ok:
                    selected_proof = mihomo_runtime_fixed_target_materialized(
                        desired["routing"], require_selected=True,
                    )
                    if not selected_proof.get("ok") or not _fixed_apply_token_matches(
                        server_id=server_id,
                        fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                        token=token,
                    ):
                        apply_result = None
                        stale_before_native = True
                else:
                    selected_proof = None

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
            "post_check": None, "rolled_back": False,
            "post_check_failed_no_rollback": False,
            "error_code": "GLOBAL_FIXED_SERVER_STALE",
            "error_message": "Fixed routing changed during runtime verification; no completion was recorded.",
        }

    if not apply_result.ok or not selected_proof.get("ok"):
        failure_code = apply_result.error_code if not apply_result.ok else "GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED"
        failure_message = apply_result.error_message or apply_result.message if not apply_result.ok else "Fixed target native selection was not verified."
        rollback = None
        with xray_writer_guard(timeout_seconds=30.0):
            if _fixed_apply_token_matches(
                server_id=server_id,
                fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                token=token,
            ):
                # Restore prior durable intent only when the adapter confirms
                # that the failed native request left the active selector as it
                # was. A partial or uncertain effect remains explicitly failed.
                if not apply_result.ok and DEFAULT_MIHOMO_ADAPTER.get_active_server_id() == active_before:
                    rollback = _restore_global_routing_state(
                        previous_state,
                        expected_revision=int(token["selection_revision"]),
                        expected_server_id=server_id,
                        expected_fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                    )
                if rollback is None or not rollback.get("ok"):
                    with db_session() as connection:
                        connection.execute(
                            """UPDATE routing_global_state SET apply_state='failed', error_code=?,
                                      error_message=?, updated_at=CURRENT_TIMESTAMP
                               WHERE id=1 AND server_mode='fixed' AND desired_fixed_server_id=?
                                 AND fixed_server_until=? AND apply_state='pending'""",
                            (failure_code, failure_message, server_id, desired["routing"].get("fixed_server_until")),
                        )
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
            "post_check": None, "rolled_back": bool(rollback and rollback.get("ok")),
            "post_check_failed_no_rollback": False,
            "error_code": failure_code, "error_message": failure_message,
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

    with xray_writer_guard(timeout_seconds=30.0), db_session() as connection:
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision
        if not _fixed_apply_token_matches(
            server_id=server_id,
            fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
            token=token,
        ):
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution, "server_id": server_id,
                "active_before": active_before,
                "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "GLOBAL_FIXED_SERVER_STALE",
                "error_message": "Fixed routing changed before completion; state remains unverified.",
            }
        final_native_proof = mihomo_runtime_fixed_target_materialized(
            desired["routing"], require_selected=True,
        )
        if (
            not final_native_proof.get("ok")
            or final_native_proof.get("fixed_runtime_target") != mihomo_target
            or not _fixed_apply_token_matches(
                server_id=server_id,
                fixed_until=str(desired["routing"].get("fixed_server_until") or ""),
                token=token,
            )
        ):
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution, "server_id": server_id,
                "active_before": active_before,
                "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "fixed_materialization_proof": final_native_proof,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED",
                "error_message": "Fixed target no longer matches native runtime readback at completion.",
            }
        changed = connection.execute(
            """
            UPDATE routing_global_state
            SET
                server_mode = 'fixed',
                desired_fixed_server_id = ?,
                applied_fixed_server_id = ?,
                fixed_server_until = ?,
                apply_state = ?,
                error_code = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1 AND server_mode='fixed' AND desired_fixed_server_id=?
              AND fixed_server_until=? AND apply_state='pending'
            """,
            (
                server_id,
                server_id,
                desired["routing"].get("fixed_server_until"),
                apply_state,
                error_code,
                error_message,
                server_id,
                desired["routing"].get("fixed_server_until"),
            ),
        )
        if changed.rowcount != 1:
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution, "server_id": server_id,
                "active_before": active_before,
                "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "GLOBAL_FIXED_SERVER_STALE",
                "error_message": "Fixed routing intent no longer owns completion.",
            }
        if advance_selection_revision(
            connection, expected_revision=token["selection_revision"],
        ) is None:
            connection.rollback()
            return {
                "ok": False, "requested_by": requested_by,
                "management_attribution": attribution, "server_id": server_id,
                "active_before": active_before,
                "active_after": DEFAULT_MIHOMO_ADAPTER.get_active_server_id(),
                "routing_before": previous_state,
                "routing_after": get_routing_global_state(expire_ttl=False),
                "server": validation["server"], "pre_check": pre_check,
                "apply_result": apply_result.to_dict(), "post_check": post_check_result,
                "rolled_back": False, "post_check_failed_no_rollback": False,
                "error_code": "GLOBAL_FIXED_SERVER_STALE",
                "error_message": "Fixed routing completion lost its selection revision fence.",
            }

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

    token = _capture_fixed_apply_context(desired["routing"])
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
