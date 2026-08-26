from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptRunnerError
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logs import write_technical_log
from fwrouter_api.services.traffic import record_traffic_samples
from fwrouter_api.services.ui_display_settings import (
    UI_DISPLAY_SETTINGS_KEY,
    _json_loads,
    custom_external_system_by_id,
    external_connection_contract,
)

ALLOWED_FILE_ROOT = Path("/var/lib/fwrouter-v2/external-collectors").resolve()
MAX_COLLECTOR_RESPONSE_BYTES = 256 * 1024

_SCHEDULER_THREAD: threading.Thread | None = None
_SCHEDULER_STOP = threading.Event()
_LAST_RUN_AT: dict[str, float] = {}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_json_loads(raw: str) -> dict[str, Any]:
    loaded = json.loads(raw)
    if isinstance(loaded, dict):
        return loaded
    if isinstance(loaded, list):
        return {"items": loaded}
    raise ValueError("Collector JSON must be an object or list.")


def _load_interval_external_systems() -> list[dict[str, Any]]:
    from fwrouter_api.services.external_connections_registry import list_external_connections

    try:
        with db_session() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?",
                (UI_DISPLAY_SETTINGS_KEY,),
            ).fetchone()
    except sqlite3.OperationalError:
        return []
    settings = _json_loads(row["value_json"]) if row else {}
    settings = settings if isinstance(settings, dict) else {}
    visibility = settings.get("system_visibility")
    visibility = visibility if isinstance(visibility, dict) else {}
    systems = []
    for system in list_external_connections(enabled_only=True):
        system_id = str(system.get("system_id") or "")
        if visibility.get(system_id) is False:
            continue
        if system.get("refresh_mode") != "interval":
            continue
        if system.get("integration_mode") == "api_push":
            continue
        systems.append(system)
    return systems


def _collector_config(system: dict[str, Any]) -> dict[str, Any]:
    config = system.get("collector_config")
    return config if isinstance(config, dict) else {}


def _read_http_json(url: str, timeout_seconds: int) -> dict[str, Any]:
    request = Request(url, headers={"accept": "application/json", "user-agent": "FWRouterExternalCollector/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read(MAX_COLLECTOR_RESPONSE_BYTES + 1)
        if len(raw) > MAX_COLLECTOR_RESPONSE_BYTES:
            raise ValueError("Collector response is too large.")
        if response.status >= 400:
            raise ValueError(f"Collector returned HTTP {response.status}.")
    return _safe_json_loads(raw.decode("utf-8"))


def _read_file_json(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_relative_to(ALLOWED_FILE_ROOT):
        raise ValueError(f"Collector file must be inside {ALLOWED_FILE_ROOT}.")
    raw = path.read_text(encoding="utf-8")[: MAX_COLLECTOR_RESPONSE_BYTES + 1]
    if len(raw) > MAX_COLLECTOR_RESPONSE_BYTES:
        raise ValueError("Collector file is too large.")
    return _safe_json_loads(raw)


def _run_command_json(script_id: str, *, extra_args: list[str], timeout_seconds: int) -> dict[str, Any]:
    result = DEFAULT_SCRIPT_RUNNER.run(
        script_id,
        extra_args=extra_args,
        timeout_seconds=timeout_seconds,
    )
    if not result.ok:
        raise ValueError(result.stderr.strip() or f"Collector script failed: {script_id}")
    return _safe_json_loads(result.stdout)


def _payload_samples(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_samples = payload.get("traffic_samples") or payload.get("samples") or []
    if not isinstance(raw_samples, list):
        return []
    return [item for item in raw_samples if isinstance(item, dict)]


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    clients = payload.get("clients")
    items = payload.get("items")
    return {
        "status": str(payload.get("status") or payload.get("state") or "ok"),
        "clients_count": len(clients) if isinstance(clients, list) else None,
        "items_count": len(items) if isinstance(items, list) else None,
        "traffic_samples_count": len(_payload_samples(payload)),
        "details": payload.get("details") if isinstance(payload.get("details"), dict) else {},
    }


def run_external_connection_collector(
    system_id: str,
    *,
    dry_run: bool = True,
    requested_by: str = "external_collector",
) -> dict[str, Any]:
    system = custom_external_system_by_id(system_id)
    if not system:
        contract = external_connection_contract(system_id)
        system = contract if isinstance(contract, dict) else None
    if not system:
        return {"ok": False, "error_code": "EXTERNAL_CONNECTION_NOT_FOUND"}

    integration_mode = str(system.get("integration_mode") or "api_push")
    refresh_mode = str(system.get("refresh_mode") or "on_change")
    config = _collector_config(system)
    identity = system.get("identity") if isinstance(system.get("identity"), dict) else {}
    collector_name = str(identity.get("collector") or f"external_connection:{system.get('connection_id') or system.get('system_id')}")

    if integration_mode == "api_push":
        return {
            "ok": True,
            "skipped": True,
            "reason": "api_push_waits_for_external_updates",
            "system_id": system.get("system_id"),
            "connection_id": system.get("connection_id") or system.get("system_id"),
            "integration_mode": integration_mode,
            "refresh_mode": refresh_mode,
            "collected_at": _utc_timestamp(),
        }

    try:
        timeout_seconds = int(config.get("timeout_seconds") or 5)
        if integration_mode == "http_poll":
            payload = _read_http_json(str(config.get("url") or ""), timeout_seconds)
        elif integration_mode == "file_read":
            payload = _read_file_json(str(config.get("path") or ""))
        elif integration_mode == "command_probe":
            extra_args = config.get("extra_args") if isinstance(config.get("extra_args"), list) else []
            payload = _run_command_json(
                str(config.get("script_id") or ""),
                extra_args=[str(item) for item in extra_args],
                timeout_seconds=timeout_seconds,
            )
        else:
            return {"ok": False, "error_code": "UNSUPPORTED_INTEGRATION_MODE", "integration_mode": integration_mode}
    except (OSError, URLError, ValueError, ScriptRunnerError, json.JSONDecodeError) as exc:
        failure = {
            "ok": False,
            "error_code": "EXTERNAL_COLLECTOR_FAILED",
            "error_message": str(exc),
            "system_id": system.get("system_id"),
            "connection_id": system.get("connection_id") or system.get("system_id"),
            "integration_mode": integration_mode,
            "refresh_mode": refresh_mode,
        }
        write_technical_log(
            component="external-collector",
            level="warning",
            event_type="external_collector_failed",
            message="External connection collector failed.",
            details=failure,
            dedupe_key=f"external_collector_failed:{system.get('system_id')}:{integration_mode}:{str(exc)[:120]}",
            cooldown_seconds=300,
        )
        return failure

    traffic_result = None
    samples = _payload_samples(payload)
    if samples and bool(config.get("apply_traffic")) and not dry_run:
        traffic_result = record_traffic_samples(samples, collector=collector_name, dry_run=False)

    return {
        "ok": True,
        "system_id": system.get("system_id"),
        "connection_id": system.get("connection_id") or system.get("system_id"),
        "integration_mode": integration_mode,
        "refresh_mode": refresh_mode,
        "dry_run": dry_run,
        "collected_at": _utc_timestamp(),
        "payload_summary": _payload_summary(payload),
        "traffic_result": traffic_result,
    }


def run_due_external_collectors_once(*, now: float | None = None) -> list[dict[str, Any]]:
    current = time.monotonic() if now is None else now
    results: list[dict[str, Any]] = []
    for system in _load_interval_external_systems():
        system_id = str(system.get("system_id") or "")
        config = _collector_config(system)
        interval = max(30, int(config.get("interval_seconds") or 300))
        last_run = _LAST_RUN_AT.get(system_id, 0.0)
        if current - last_run < interval:
            continue
        _LAST_RUN_AT[system_id] = current
        results.append(run_external_connection_collector(system_id, dry_run=False, requested_by="external_collector_scheduler"))
    return results


def _external_collector_scheduler_loop() -> None:
    settings = get_settings()
    interval = settings.external_collector_check_interval_seconds
    while not _SCHEDULER_STOP.wait(interval):
        try:
            run_due_external_collectors_once()
        except Exception as exc:
            write_technical_log(
                component="external-collector",
                level="error",
                event_type="external_collector_scheduler_failed",
                message="External collector scheduler failed.",
                details={"error_message": str(exc)},
            )


def start_external_collector_scheduler() -> bool:
    global _SCHEDULER_THREAD
    settings = get_settings()
    if not settings.external_collector_scheduler_enabled:
        return False
    if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
        return True
    _SCHEDULER_STOP.clear()
    _SCHEDULER_THREAD = threading.Thread(
        target=_external_collector_scheduler_loop,
        name="fwrouter-external-collector-scheduler",
        daemon=True,
    )
    _SCHEDULER_THREAD.start()
    return True


def stop_external_collector_scheduler(*, timeout_seconds: float = 2.0) -> bool:
    _SCHEDULER_STOP.set()
    thread = _SCHEDULER_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=timeout_seconds)
    return not (thread and thread.is_alive())
