from __future__ import annotations

from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.tailscale import probe_tailscale_runtime, run_tailscale_lifecycle_action


VALID_DESIRED_STATES = {"enabled", "disabled"}
TAILSCALE_ACTIONS = {"start", "stop", "restart"}


class ModuleNotFoundError(ValueError):
    """Raised when requested FWRouter module does not exist."""


class ModuleStateError(ValueError):
    """Raised when requested module state transition is invalid."""


def _apply_config_runtime_overrides(module: dict[str, Any]) -> dict[str, Any]:
    if module.get("module_name") != "watchdog":
        return module

    settings = get_settings()
    if settings.watchdog_scheduler_enabled:
        return module

    overridden = dict(module)
    overridden["runtime_state"] = "stopped"
    overridden["apply_state"] = "clean"
    overridden["status_text"] = "Watchdog scheduler is disabled by config."
    overridden["error_code"] = "WATCHDOG_DISABLED_BY_CONFIG"
    overridden["error_message"] = "FWROUTER_WATCHDOG_SCHEDULER_ENABLED is false."
    return overridden


def fetch_modules() -> list[dict[str, Any]]:
    """Return all FWRouter module states ordered by module name."""

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                module_name,
                desired_state,
                runtime_state,
                apply_state,
                status_text,
                error_code,
                error_message,
                updated_at
            FROM modules
            ORDER BY module_name
            """
        ).fetchall()

    return [
        _apply_config_runtime_overrides(
            {
                "module_name": row["module_name"],
                "desired_state": row["desired_state"],
                "runtime_state": row["runtime_state"],
                "apply_state": row["apply_state"],
                "status_text": row["status_text"],
                "error_code": row["error_code"],
                "error_message": row["error_message"],
                "updated_at": row["updated_at"],
            }
        )
        for row in rows
    ]


def find_module(
    modules: list[dict[str, Any]],
    module_name: str,
) -> dict[str, Any] | None:
    """Find one module DTO in an already loaded module list."""

    for module in modules:
        if module["module_name"] == module_name:
            return module
    return None


def get_module_state(module_name: str) -> dict[str, Any] | None:
    """Return one module state from SQLite."""

    return find_module(fetch_modules(), module_name)


def _update_module_state(
    module_name: str,
    *,
    desired_state: str,
    runtime_state: str | None = None,
    apply_state: str | None = None,
    status_text: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Update one module state and return the updated module DTO."""

    updates = [
        "desired_state = ?",
        "error_code = ?",
        "error_message = ?",
        "updated_at = CURRENT_TIMESTAMP",
    ]
    values: list[Any] = [desired_state, error_code, error_message]

    if runtime_state is not None:
        updates.append("runtime_state = ?")
        values.append(runtime_state)

    if apply_state is not None:
        updates.append("apply_state = ?")
        values.append(apply_state)

    if status_text is not None:
        updates.append("status_text = ?")
        values.append(status_text)

    values.append(module_name)

    with db_session() as connection:
        connection.execute(
            f"""
            UPDATE modules
            SET {", ".join(updates)}
            WHERE module_name = ?
            """,
            values,
        )

    module = get_module_state(module_name)
    if module is None:
        raise ModuleNotFoundError(f"Module not found: {module_name}")

    return module


def set_module_desired_state(
    module_name: str,
    desired_state: str,
    *,
    requested_by: str = "api",
    run_now: bool = True,
) -> dict[str, Any]:
    """Set module desired state.

    Enabling the VPN module prepares fresh subscription inventory through a
    safe job. The job refreshes servers, generates and validates a Mihomo
    candidate config. It does not promote config and does not restart Mihomo.
    """

    if desired_state not in VALID_DESIRED_STATES:
        raise ModuleStateError(f"Invalid desired state: {desired_state}")

    current = get_module_state(module_name)
    if current is None:
        raise ModuleNotFoundError(f"Module not found: {module_name}")

    status_text = f"Module {module_name} desired state set to {desired_state}."
    job: dict[str, Any] | None = None

    module = _update_module_state(
        module_name,
        desired_state=desired_state,
        apply_state="pending" if desired_state == "enabled" else "clean",
        status_text=status_text,
    )

    if module_name == "vpn" and desired_state == "enabled":
        manager = get_default_job_manager()
        job = manager.create(
            "subscription_refresh_prepare",
            lock_key="subscription_refresh",
            requested_by=requested_by,
            input_data={
                "reason": "vpn_module_enable",
                "module_name": module_name,
            },
        )

        if run_now:
            job = manager.start_job_and_wait(job["job_id"]) or job

        if job.get("status") == "success":
            module = _update_module_state(
                module_name,
                desired_state=desired_state,
                runtime_state="running",
                apply_state="clean",
                status_text=(
                    "VPN module enabled. Subscription inventory refreshed and "
                    "Mihomo candidate config validated."
                ),
            )
        elif job.get("status") == "running":
            module = _update_module_state(
                module_name,
                desired_state=desired_state,
                apply_state="pending",
                status_text="VPN module enable job is still running. Poll job status for completion.",
                error_code=None,
                error_message=None,
            )
        elif run_now:
            module = _update_module_state(
                module_name,
                desired_state=desired_state,
                apply_state="failed",
                status_text="VPN module enable failed during subscription refresh.",
                error_code=job.get("error_code") or "VPN_MODULE_ENABLE_FAILED",
                error_message=job.get("error_message"),
            )

    if module_name == "watchdog":
        if desired_state == "enabled":
            module = _update_module_state(
                module_name,
                desired_state=desired_state,
                runtime_state="paused",
                apply_state="clean",
                status_text=(
                    "Watchdog automation enabled. Waiting for VPN auto-path activity."
                ),
                error_code=None,
                error_message=None,
            )
        else:
            module = _update_module_state(
                module_name,
                desired_state=desired_state,
                runtime_state="stopped",
                apply_state="clean",
                status_text="Watchdog automation disabled.",
                error_code=None,
                error_message=None,
            )

    if module_name == "tailscale":
        if desired_state == "enabled":
            from fwrouter_api.jobs.extended_handlers import register_extended_handlers

            manager = get_default_job_manager()
            register_extended_handlers(manager)
            job = manager.create(
                "subject_inventory_sync",
                lock_key="subject_inventory_sync",
                requested_by=requested_by,
                input_data={
                    "reason": "tailscale_module_enable",
                    "module_name": module_name,
                    "discover_docker": False,
                    "discover_tailscale": True,
                    "discover_xray": False,
                    "include_all_tailscale_peers": False,
                },
            )

            if run_now:
                job = manager.start_job_and_wait(job["job_id"]) or job

            if run_now:
                sync_result = (
                    ((job.get("result") or {}).get("subjects"))
                    if isinstance(job.get("result"), dict)
                    else None
                )
                tailscale_probe = probe_tailscale_runtime()
                warnings = (
                    list(sync_result.get("warnings") or [])
                    if isinstance(sync_result, dict)
                    else []
                )
                imported_count = 0
                if isinstance(sync_result, dict):
                    imported_count = int((sync_result.get("synced_counts") or {}).get("tailscale_node", 0) or 0)

                if job.get("status") == "success" and tailscale_probe["ok"] and not warnings:
                    module = _update_module_state(
                        module_name,
                        desired_state=desired_state,
                        runtime_state="running",
                        apply_state="clean",
                        status_text=(
                            "Tailscale module enabled. Host status probe succeeded and "
                            f"{imported_count} tailscale_node subjects were synced."
                        ),
                        error_code=None,
                        error_message=None,
                    )
                elif job.get("status") == "running":
                    module = _update_module_state(
                        module_name,
                        desired_state=desired_state,
                        runtime_state="paused",
                        apply_state="pending",
                        status_text=(
                            "Tailscale module sync job is still running. Poll job status for completion."
                        ),
                        error_code=None,
                        error_message=None,
                    )
                else:
                    first_warning = warnings[0] if warnings else {}
                    module = _update_module_state(
                        module_name,
                        desired_state=desired_state,
                        runtime_state="degraded",
                        apply_state="failed",
                        status_text=(
                            "Tailscale module enable finished with degraded runtime visibility. "
                            "Check tailscale_status probe and inventory sync warnings."
                        ),
                        error_code=(
                            str(first_warning.get("error_code") or tailscale_probe.get("error_code") or job.get("error_code") or "TAILSCALE_MODULE_ENABLE_FAILED")
                        ),
                        error_message=(
                            str(first_warning.get("message") or tailscale_probe.get("error_message") or job.get("error_message") or "Tailscale module enable failed.")
                        ),
                    )
        else:
            module = _update_module_state(
                module_name,
                desired_state=desired_state,
                runtime_state="paused",
                apply_state="clean",
                status_text=(
                    "Tailscale module disabled in FWRouter control plane. "
                    "Host Tailscale service remains unmanaged by FWRouter."
                ),
                error_code=None,
                error_message=None,
            )

    return {
        "module": module,
        "job": job,
    }


def run_module_action(
    module_name: str,
    action: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    current = get_module_state(module_name)
    if current is None:
        raise ModuleNotFoundError(f"Module not found: {module_name}")

    normalized_action = action.strip().lower()
    if module_name != "tailscale" or normalized_action not in TAILSCALE_ACTIONS:
        raise ModuleStateError(
            "Only tailscale module actions are supported, and only: start, stop, restart."
        )

    action_result = run_tailscale_lifecycle_action(normalized_action)
    runtime = action_result.get("runtime") if isinstance(action_result.get("runtime"), dict) else {}
    runtime_state = str(runtime.get("runtime_state") or "degraded")
    ok = bool(action_result.get("ok"))
    module = _update_module_state(
        module_name,
        desired_state=current["desired_state"],
        runtime_state=runtime_state,
        apply_state="clean" if ok else "failed",
        status_text=(
            f"Tailscale lifecycle action `{normalized_action}` completed."
            if ok
            else f"Tailscale lifecycle action `{normalized_action}` failed."
        ),
        error_code=None if ok else str(action_result.get("error_code") or "TAILSCALE_ACTION_FAILED"),
        error_message=None if ok else str(action_result.get("error_message") or "Tailscale action failed."),
    )
    return {
        "module": module,
        "action": normalized_action,
        "action_result": action_result,
        "requested_by": requested_by,
    }
