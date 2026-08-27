from __future__ import annotations

from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import get_default_job_manager
from fwrouter_api.services.external_ingress import probe_external_ingress_runtime
from fwrouter_api.services.subject_taxonomy import external_ingress_contract_by_module


VALID_DESIRED_STATES = {"enabled", "disabled"}
VALID_LIFECYCLE_MODES = {"none", "managed", "external"}
MODULE_LIFECYCLE_ALLOWED = {
    "core": {"managed"},
    "vpn": {"none", "managed", "external"},
    "xray": {"none", "managed", "external"},
    "tailscale": {"none", "external"},
    "watchdog": {"managed"},
    "selector": {"managed"},
    "subscription": {"managed"},
}
MANAGED_INSTALL_MARKERS = {
    "core": (Path("/opt/fwrouter-api"),),
    "vpn": (Path("/opt/fwrouter-mihomo/docker-compose.yml"),),
    "xray": (Path("/opt/fwrouter-xray/docker-compose.yml"),),
    "watchdog": (Path("/opt/fwrouter-api"),),
    "selector": (Path("/opt/fwrouter-api"),),
    "subscription": (Path("/opt/fwrouter-api"),),
}


class ModuleNotFoundError(ValueError):
    """Raised when requested FWRouter module does not exist."""


class ModuleStateError(ValueError):
    """Raised when requested module state transition is invalid."""


def require_managed_module(module_name: str) -> dict[str, Any]:
    """Return module state if FWRouter owns its runtime lifecycle."""

    current = get_module_state(module_name)
    if current is None:
        raise ModuleNotFoundError(f"Module not found: {module_name}")

    lifecycle_mode = str(current.get("lifecycle_mode") or "none")
    if lifecycle_mode != "managed":
        raise ModuleStateError(
            "Managed runtime operation requires lifecycle_mode=managed. "
            "External integrations are user-managed and FWRouter must not "
            "create runtime files, restart services, or reload containers for them."
        )

    return current


def managed_runtime_operation_blocked(
    module_name: str,
    *,
    error_code: str,
    operation: str,
) -> dict[str, Any] | None:
    """Return a common blocked-operation payload unless module is managed."""

    try:
        require_managed_module(module_name)
    except ModuleNotFoundError as exc:
        message = str(exc)
        return {
            "ok": False,
            "status": "blocked",
            "stage": "lifecycle",
            "operation": operation,
            "module": None,
            "error_code": "MODULE_NOT_FOUND",
            "error_message": message,
            "error": {
                "code": "MODULE_NOT_FOUND",
                "message": message,
            },
            "result": {
                "error_code": "MODULE_NOT_FOUND",
                "message": message,
                "details": {"module_name": module_name, "operation": operation},
            },
        }
    except ModuleStateError as exc:
        module = get_module_state(module_name)
        message = str(exc)
        return {
            "ok": False,
            "status": "blocked",
            "stage": "lifecycle",
            "operation": operation,
            "module": module,
            "error_code": error_code,
            "error_message": message,
            "error": {
                "code": error_code,
                "message": message,
            },
            "result": {
                "error_code": error_code,
                "message": message,
                "details": {
                    "module_name": module_name,
                    "operation": operation,
                    "lifecycle_mode": (module or {}).get("lifecycle_mode"),
                },
            },
        }
    return None


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


def _module_installed(module_name: str, lifecycle_mode: str) -> bool:
    if lifecycle_mode == "none":
        return False
    if lifecycle_mode == "external":
        return True
    markers = MANAGED_INSTALL_MARKERS.get(module_name, ())
    return bool(markers) and all(marker.exists() for marker in markers)


def _module_manageable_actions(module_name: str, lifecycle_mode: str) -> list[str]:
    return []


def _enrich_module(module: dict[str, Any]) -> dict[str, Any]:
    lifecycle_mode = str(module.get("lifecycle_mode") or "none")
    module_name = str(module.get("module_name") or "")
    enriched = dict(module)
    enriched["lifecycle_mode"] = lifecycle_mode
    enriched["installed"] = _module_installed(module_name, lifecycle_mode)
    enriched["manageable_actions"] = _module_manageable_actions(module_name, lifecycle_mode)
    return enriched


def fetch_modules() -> list[dict[str, Any]]:
    """Return all FWRouter module states ordered by module name."""

    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                module_name,
                desired_state,
                lifecycle_mode,
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

    modules = [
        _apply_config_runtime_overrides(
            {
                "module_name": row["module_name"],
                "desired_state": row["desired_state"],
                "lifecycle_mode": row["lifecycle_mode"],
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
    return [_enrich_module(module) for module in modules]


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


def set_module_lifecycle_mode(
    module_name: str,
    lifecycle_mode: str,
) -> dict[str, Any]:
    """Set how FWRouter relates to one integration lifecycle."""

    normalized_mode = lifecycle_mode.strip().lower()
    if normalized_mode not in VALID_LIFECYCLE_MODES:
        raise ModuleStateError(f"Invalid lifecycle mode: {lifecycle_mode}")

    current = get_module_state(module_name)
    if current is None:
        raise ModuleNotFoundError(f"Module not found: {module_name}")

    allowed = MODULE_LIFECYCLE_ALLOWED.get(module_name, {"managed"})
    if normalized_mode not in allowed:
        raise ModuleStateError(
            f"Lifecycle mode {normalized_mode} is not supported for module {module_name}."
        )

    runtime_state = "not_configured" if normalized_mode == "none" else current["runtime_state"]
    apply_state = "clean" if normalized_mode == "none" else current["apply_state"]
    status_text = (
        f"Module {module_name} lifecycle mode set to {normalized_mode}."
        if normalized_mode != "none"
        else f"Module {module_name} integration is not installed in FWRouter."
    )

    with db_session() as connection:
        connection.execute(
            """
            UPDATE modules
            SET
                lifecycle_mode = ?,
                runtime_state = ?,
                apply_state = ?,
                status_text = ?,
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE module_name = ?
            """,
            (normalized_mode, runtime_state, apply_state, status_text, module_name),
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

    external_ingress_contract = external_ingress_contract_by_module(module_name)
    if external_ingress_contract is not None:
        provider = str(external_ingress_contract["provider"])
        label = str(external_ingress_contract.get("display_label") or provider)
        subject_type = str(external_ingress_contract["subject_type"])
        if desired_state == "enabled":
            from fwrouter_api.services.external_connections_registry import list_external_connections
            from fwrouter_api.jobs.extended_handlers import register_extended_handlers

            ingress_connections = [
                connection
                for connection in list_external_connections(enabled_only=True)
                if str(connection.get("connection_type") or "") == "external_network_source"
                and str(connection.get("runtime_type") or "").strip().lower() == provider
            ]
            if not ingress_connections:
                return _update_module_state(
                    module_name,
                    desired_state=desired_state,
                    runtime_state="degraded",
                    apply_state="failed",
                    status_text=(
                        "External ingress module requires a registered external network connection."
                    ),
                    error_code="EXTERNAL_INGRESS_CONNECTION_REQUIRED",
                    error_message=f"No enabled external network connection is registered for {label}.",
                )

            manager = get_default_job_manager()
            register_extended_handlers(manager)
            job = manager.create(
                "subject_inventory_sync",
                lock_key="subject_inventory_sync",
                requested_by=requested_by,
                input_data={
                    "reason": f"{provider}_external_ingress_enable",
                    "module_name": module_name,
                    "discover_docker": False,
                    "discover_external_ingress_providers": [provider],
                    "discover_xray": False,
                    "include_all_external_ingress_peers": False,
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
                warnings = (
                    list(sync_result.get("warnings") or [])
                    if isinstance(sync_result, dict)
                    else []
                )
                imported_count = 0
                if isinstance(sync_result, dict):
                    imported_count = int(
                        (sync_result.get("synced_counts") or {}).get(subject_type, 0) or 0
                    )

                probe_connection = ingress_connections[0]
                provider_probe = probe_external_ingress_runtime(
                    provider,
                    connection_id=str(probe_connection.get("connection_id") or ""),
                    collector_config=(
                        probe_connection.get("collector_config")
                        if isinstance(probe_connection.get("collector_config"), dict)
                        else {}
                    ),
                )

                if job.get("status") == "success" and provider_probe["ok"] and not warnings:
                    module = _update_module_state(
                        module_name,
                        desired_state=desired_state,
                        runtime_state="running",
                        apply_state="clean",
                        status_text=(
                            "External ingress module enabled. Host status probe succeeded and "
                            f"{imported_count} {subject_type} subjects were synced."
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
                            "External ingress module sync job is still running. Poll job status for completion."
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
                            "External ingress module enable finished with degraded runtime visibility. "
                            "Check provider probe and inventory sync warnings."
                        ),
                        error_code=(
                            str(
                                first_warning.get("error_code")
                                or provider_probe.get("error_code")
                                or job.get("error_code")
                                or "EXTERNAL_INGRESS_MODULE_ENABLE_FAILED"
                            )
                        ),
                        error_message=(
                            str(
                                first_warning.get("message")
                                or provider_probe.get("error_message")
                                or job.get("error_message")
                                or f"{label} external ingress module enable failed."
                            )
                        ),
                    )
        else:
            module = _update_module_state(
                module_name,
                desired_state=desired_state,
                runtime_state="paused",
                apply_state="clean",
                status_text=(
                    "External ingress module disabled in FWRouter control plane. "
                    "Provider runtime lifecycle remains unmanaged by FWRouter."
                ),
                error_code=None,
                error_message=None,
            )

    return {
        "module": module,
        "job": job,
    }
