from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any

from fwrouter_api.db.connection import db_session


XRAY_VPN_AUTO_PENDING_KEY = "xray.vpn_auto_pending"
XRAY_VPN_AUTO_RECONCILE_JOB = "xray_vpn_auto_reconcile"
XRAY_VPN_AUTO_RECONCILE_LOCK = "xray-vpn-auto-reconcile"
XRAY_VPN_AUTO_DEBOUNCE_SECONDS = 180
XRAY_VPN_AUTO_RETRY_SECONDS = 60


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    current = _now(value)
    timespec = "microseconds" if current.microsecond else "seconds"
    return current.isoformat(timespec=timespec).replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _now(parsed)


def _load_row(connection: Any) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT value_json FROM settings WHERE key = ?",
        (XRAY_VPN_AUTO_PENDING_KEY,),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _save(connection: Any, value: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO settings (key, value_json, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (XRAY_VPN_AUTO_PENDING_KEY, json.dumps(value, ensure_ascii=False, sort_keys=True)),
    )


def _save_if_revision(connection: Any, value: dict[str, Any], revision: int) -> bool:
    cursor = connection.execute(
        """
        UPDATE settings
        SET value_json = ?, updated_at = CURRENT_TIMESTAMP
        WHERE key = ?
          AND CAST(json_extract(value_json, '$.revision') AS INTEGER) = ?
          AND json_extract(value_json, '$.pending') = 1
        """,
        (
            json.dumps(value, ensure_ascii=False, sort_keys=True),
            XRAY_VPN_AUTO_PENDING_KEY,
            int(revision),
        ),
    )
    return cursor.rowcount == 1


def mark_xray_vpn_auto_pending(
    connection: Any,
    *,
    trigger: str,
    now: datetime | None = None,
    immediate: bool = False,
) -> dict[str, Any]:
    """Record a trailing-debounce revision in the caller's intent transaction."""

    current = _now(now)
    if not bool(getattr(connection, "in_transaction", False)):
        connection.execute("BEGIN IMMEDIATE")
    previous = _load_row(connection) or {}
    revision = max(0, int(previous.get("revision") or 0)) + 1
    value = {
        "revision": revision,
        "pending": True,
        "status": "pending",
        "due_at": _timestamp(current + timedelta(seconds=0 if immediate else XRAY_VPN_AUTO_DEBOUNCE_SECONDS)),
        "trigger": str(trigger or "manual_preferences")[:64],
        "updated_at": _timestamp(current),
        "attempt_count": 0,
        "last_attempt_at": None,
        "last_error_code": None,
        "next_retry_at": None,
        "applied_revision": int(previous.get("applied_revision") or 0),
    }
    _save(connection, value)
    return value


def get_xray_vpn_auto_pending_state() -> dict[str, Any]:
    with db_session() as connection:
        value = _load_row(connection)
    if not isinstance(value, dict):
        return {
            "revision": 0,
            "pending": False,
            "status": "idle",
            "due_at": None,
            "trigger": None,
            "last_attempt_at": None,
            "last_error_code": None,
            "next_retry_at": None,
            "applied_revision": 0,
        }
    return {
        key: value.get(key)
        for key in (
            "revision",
            "pending",
            "status",
            "due_at",
            "trigger",
            "updated_at",
            "last_attempt_at",
            "last_error_code",
            "next_retry_at",
            "applied_revision",
        )
    } | {"attempt_count": int(value.get("attempt_count") or 0)}


def pending_is_due(state: dict[str, Any], *, now: datetime | None = None, bypass: bool = False) -> bool:
    if not bool(state.get("pending")):
        return False
    current = _now(now)
    retry = _parse_timestamp(state.get("next_retry_at"))
    if str(state.get("status") or "") == "failed" and retry is not None and current < retry:
        return False
    if bypass:
        return True
    if str(state.get("status") or "") == "deferred":
        return True
    due = _parse_timestamp(state.get("due_at"))
    return due is not None and current >= max(due, retry or due)


def _begin_attempt(revision: int, *, now: datetime | None = None) -> dict[str, Any] | None:
    current = _now(now)
    with db_session() as connection:
        state = _load_row(connection)
        if not state or not bool(state.get("pending")) or int(state.get("revision") or 0) != revision:
            return None
        state.update(
            {
                "status": "running",
                "attempt_count": int(state.get("attempt_count") or 0) + 1,
                "last_attempt_at": _timestamp(current),
                "next_retry_at": None,
                "updated_at": _timestamp(current),
            }
        )
        if not _save_if_revision(connection, state, revision):
            return None
        return state


def _finish_revision(
    revision: int,
    *,
    ok: bool,
    error_code: str | None = None,
    now: datetime | None = None,
) -> bool:
    current = _now(now)
    with db_session() as connection:
        state = _load_row(connection)
        if not state or not bool(state.get("pending")) or int(state.get("revision") or 0) != revision:
            return False
        state["updated_at"] = _timestamp(current)
        if ok:
            state.update(
                {
                    "pending": False,
                    "status": "applied",
                    "due_at": None,
                    "next_retry_at": None,
                    "last_error_code": None,
                    "applied_revision": revision,
                }
            )
        else:
            state.update(
                {
                    "pending": True,
                    "status": "failed",
                    "due_at": _timestamp(current),
                    "last_error_code": str(error_code or "XRAY_VPN_AUTO_RECONCILE_FAILED")[:96],
                    "next_retry_at": _timestamp(current + timedelta(seconds=XRAY_VPN_AUTO_RETRY_SECONDS)),
                }
            )
        return _save_if_revision(connection, state, revision)


def _defer_revision(revision: int, *, now: datetime | None = None) -> bool:
    current = _now(now)
    with db_session() as connection:
        state = _load_row(connection)
        if not state or not bool(state.get("pending")) or int(state.get("revision") or 0) != revision:
            return False
        state.update(
            {
                "status": "deferred",
                "due_at": None,
                "next_retry_at": None,
                "updated_at": _timestamp(current),
            }
        )
        return _save_if_revision(connection, state, revision)


def _inventory_and_runtime_authoritative() -> bool:
    from fwrouter_api.services.subscription import get_subscription_state
    from fwrouter_api.services.xray_runtime_state import _module_state

    module = _module_state("xray") or {}
    state = get_subscription_state() or {}
    return bool(
        str(module.get("desired_state") or "") == "enabled"
        and str(module.get("lifecycle_mode") or "") == "managed"
        and str(state.get("status") or "") == "success"
        and bool(state.get("last_success_at"))
    )


def _confirmed_applied_config_drift() -> bool:
    """Only compare readable active config with the last successful applied snapshot."""

    from fwrouter_api.adapters.xray_common import xray_writer_guard

    with xray_writer_guard():
        return _confirmed_applied_config_drift_under_guard()


def _confirmed_applied_config_drift_under_guard() -> bool:

    from fwrouter_api.services.xray_materialize import (
        _load_active_config_payload,
        _verify_active_config_bindings,
        _verify_active_config_client_modes,
    )
    from fwrouter_api.services.xray_runtime_state import _load_xray_bindings_state

    applied = _load_xray_bindings_state()
    if (
        applied.get("error_code")
        or not applied.get("generated_at")
        or int(applied.get("applied_count") or 0) != int(applied.get("bindings_count") or 0)
        or not isinstance(applied.get("bindings"), list)
        or not isinstance(applied.get("client_modes"), list)
    ):
        return False
    active, load_error = _load_active_config_payload()
    if active is None or load_error is not None:
        return False

    bindings = _verify_active_config_bindings(applied["bindings"])
    modes = _verify_active_config_client_modes(applied["client_modes"])
    explicit_binding_mismatch = any(
        bindings.get(key)
        for key in ("missing_clients", "missing_outbounds", "invalid_handoffs", "missing_rules", "wrong_api_rules")
    )
    explicit_mode_mismatch = any(
        modes.get(key)
        for key in ("missing_clients", "missing_outbounds", "missing_rules", "misordered_rules")
    )
    return bool(explicit_binding_mismatch or explicit_mode_mismatch)


def clear_pending_revision_after_success(revision: int | None) -> bool:
    """Clear a bypassed/manual revision only after its full convergence path succeeds."""

    if revision is None:
        return False
    return _finish_revision(int(revision), ok=True)


def _finalize_xray_vpn_auto(*, requested_by: str) -> dict[str, Any]:
    from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime
    from fwrouter_api.services.subscription_profiles import (
        list_desired_subscription_xray_clients,
        promote_runtime_verified_subscription_nodes,
    )
    from fwrouter_api.services.xray_subscription_service import reconcile_xray_vpn_auto_subscription

    reconciled = reconcile_xray_vpn_auto_subscription(requested_by=requested_by)
    if not reconciled.get("ok"):
        return {"ok": False, "stage": "xray_reconcile", "result": reconciled}
    if str(reconciled.get("status") or "") == "skipped":
        return {"ok": True, "status": "skipped", "result": reconciled}

    final_mihomo = reconcile_mihomo_runtime() or {}
    if not final_mihomo.get("ok"):
        return {"ok": False, "stage": "final_mihomo", "result": final_mihomo}
    promoted = promote_runtime_verified_subscription_nodes(list_desired_subscription_xray_clients())
    return {
        "ok": True,
        "status": "success",
        "result": reconciled,
        "final_mihomo": final_mihomo,
        "public_profile_promote": promoted,
    }


def run_xray_vpn_auto_reconcile(
    *,
    requested_by: str,
    bypass: bool = False,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Run pending Xray reconcile under the shared writer guard and CAS completion."""

    from fwrouter_api.adapters.xray_common import xray_writer_guard

    with xray_writer_guard():
        state = get_xray_vpn_auto_pending_state()
        if not bool(state.get("pending")):
            return {"ok": True, "status": "idle", "pending": False}
        revision = int(state.get("revision") or 0)
        same_revision = expected_revision is None or int(expected_revision) == revision
        effective_bypass = bool(bypass and same_revision)
        if str(state.get("status") or "") == "deferred":
            effective_bypass = True
        elif bypass and not same_revision:
            # A queued bypass belongs to the revision that created it. Only a
            # newly confirmed mismatch against last-applied state can bypass a
            # newer revision's quiet deadline.
            effective_bypass = _confirmed_applied_config_drift()
        if not pending_is_due(state, bypass=effective_bypass):
            return {"ok": True, "status": "pending", "pending": True, "revision": state.get("revision")}

        if not _inventory_and_runtime_authoritative():
            deferred = _defer_revision(int(state.get("revision") or 0))
            return {"ok": True, "status": "deferred", "pending": deferred, "reason": "runtime_or_inventory_unavailable"}

        if _begin_attempt(revision) is None:
            return {"ok": True, "status": "superseded", "pending": True}
        try:
            result = _finalize_xray_vpn_auto(requested_by=requested_by)
        except Exception as exc:  # durable marker remains for bounded retry
            result = {
                "ok": False,
                "stage": "exception",
                "error_code": "XRAY_VPN_AUTO_RECONCILE_EXCEPTION",
                "error_message": f"{type(exc).__name__}: {exc}",
            }
        if result.get("ok") and result.get("status") == "skipped":
            _defer_revision(revision)
            current = get_xray_vpn_auto_pending_state()
            return {
                **result,
                "revision": revision,
                "cleared": False,
                "pending": bool(current.get("pending")),
                "superseded": int(current.get("revision") or 0) != revision,
            }
        if result.get("ok"):
            cleared = _finish_revision(revision, ok=True)
            current = get_xray_vpn_auto_pending_state()
            return {
                **result,
                "revision": revision,
                "cleared": cleared,
                "pending": bool(current.get("pending")),
                "superseded": int(current.get("revision") or 0) != revision,
            }
        error_code = str(result.get("error_code") or result.get("stage") or "XRAY_VPN_AUTO_RECONCILE_FAILED")
        _finish_revision(revision, ok=False, error_code=error_code)
        current = get_xray_vpn_auto_pending_state()
        return {
            **result,
            "revision": revision,
            "pending": bool(current.get("pending")),
            "superseded": int(current.get("revision") or 0) != revision,
        }


def submit_xray_vpn_auto_reconcile(
    *,
    requested_by: str,
    bypass: bool = False,
    expected_revision: int | None = None,
    run_now: bool = False,
) -> dict[str, Any]:
    from fwrouter_api.jobs.manager import get_default_job_manager
    from fwrouter_api.jobs.extended_handlers import register_extended_handlers
    from fwrouter_api.services.jobs import JobLockConflictError

    manager = get_default_job_manager()
    register_extended_handlers(manager)
    try:
        job = manager.create(
            XRAY_VPN_AUTO_RECONCILE_JOB,
            lock_key=XRAY_VPN_AUTO_RECONCILE_LOCK,
            requested_by=requested_by,
            input_data={
                "bypass": bool(bypass),
                "expected_revision": expected_revision,
            },
        )
    except JobLockConflictError as exc:
        return {"ok": True, "status": "already_running", "job": exc.active_job}
    if run_now:
        job = manager.start_job_and_wait(job["job_id"]) or job
    else:
        job = manager.start_job(job["job_id"]) or job
    return {"ok": True, "status": "dispatched", "job": job}


def request_xray_vpn_auto_apply_now(*, requested_by: str) -> dict[str, Any]:
    with db_session() as connection:
        state = mark_xray_vpn_auto_pending(
            connection,
            trigger="explicit_apply_now",
            immediate=True,
        )
    return submit_xray_vpn_auto_reconcile(
        requested_by=requested_by,
        bypass=True,
        expected_revision=int(state["revision"]),
    )


def dispatch_due_xray_vpn_auto_reconcile(*, now: datetime | None = None) -> dict[str, Any]:
    state = get_xray_vpn_auto_pending_state()
    if not bool(state.get("pending")):
        return {"ok": True, "status": "not_pending", "pending": False}
    critical_drift = _confirmed_applied_config_drift()
    recovery_bypass = critical_drift or str(state.get("status") or "") == "deferred"
    if not pending_is_due(state, now=now, bypass=recovery_bypass):
        return {"ok": True, "status": "not_due", "pending": bool(state.get("pending")), "revision": state.get("revision")}
    if not _inventory_and_runtime_authoritative():
        revision = int(state.get("revision") or 0)
        _defer_revision(revision)
        return {"ok": True, "status": "deferred", "pending": True, "revision": revision}
    return submit_xray_vpn_auto_reconcile(
        requested_by="runtime_convergence_scheduler",
        bypass=recovery_bypass,
        expected_revision=int(state.get("revision") or 0),
    )
