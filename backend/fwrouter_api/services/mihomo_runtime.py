from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoRuntimeState


MIHOMO_COMPOSE_FILE = Path("/opt/fwrouter-mihomo/docker-compose.yml")
MIHOMO_COMPOSE_SERVICE = "mihomo"


def _run_compose_command(args: list[str], *, timeout_seconds: int = 30) -> dict[str, Any]:
    """Run docker compose command for the Mihomo runtime project."""

    command = [
        "docker",
        "compose",
        "-f",
        str(MIHOMO_COMPOSE_FILE),
        *args,
    ]

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )

    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "ok": result.returncode == 0,
    }


def get_mihomo_container_status() -> dict[str, Any]:
    """Return Docker Compose status for Mihomo without changing runtime state."""

    result = _run_compose_command(
        ["ps", MIHOMO_COMPOSE_SERVICE],
        timeout_seconds=20,
    )

    return {
        "compose_file": str(MIHOMO_COMPOSE_FILE),
        "service": MIHOMO_COMPOSE_SERVICE,
        "ok": result["ok"],
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


def wait_for_mihomo_controller(
    *,
    timeout_seconds: float = 20.0,
    poll_interval_seconds: float = 0.5,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Wait until the local Mihomo controller is reachable after restart."""

    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    last_health: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        attempts += 1
        if heartbeat is not None:
            heartbeat()
        health = DEFAULT_MIHOMO_ADAPTER.health()
        last_health = {
            "runtime_state": health.runtime_state.value,
            "active_server_id": health.active_server_id,
            "message": health.message,
            "details": health.details,
        }
        if health.runtime_state == MihomoRuntimeState.RUNNING:
            return {
                "ok": True,
                "attempts": attempts,
                "timeout_seconds": timeout_seconds,
                "health": last_health,
            }
        time.sleep(poll_interval_seconds)

    return {
        "ok": False,
        "attempts": attempts,
        "timeout_seconds": timeout_seconds,
        "health": last_health,
        "error_code": "MIHOMO_CONTROLLER_NOT_READY",
        "error_message": "Mihomo controller did not become reachable after container restart.",
    }


def _restart_mihomo_container(
    *,
    action: str = "restart",
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    from fwrouter_api.services.selector import restore_mihomo_selector_state

    if action == "force_recreate":
        restart_result = _run_compose_command(
            ["up", "-d", "--force-recreate", MIHOMO_COMPOSE_SERVICE],
            timeout_seconds=60,
        )
    elif action == "restart":
        restart_result = _run_compose_command(
            ["restart", MIHOMO_COMPOSE_SERVICE],
            timeout_seconds=60,
        )
    else:
        raise ValueError(f"Unsupported Mihomo restart action: {action}")
    if heartbeat is not None:
        heartbeat()

    status_result = get_mihomo_container_status()
    if heartbeat is not None:
        heartbeat()
    controller_wait = wait_for_mihomo_controller(heartbeat=heartbeat)
    selector_restore = None
    if controller_wait["ok"]:
        selector_restore = restore_mihomo_selector_state(requested_by=f"mihomo_restart:{action}")

    return {
        "compose_file": str(MIHOMO_COMPOSE_FILE),
        "service": MIHOMO_COMPOSE_SERVICE,
        "action": action,
        "restart": restart_result,
        "status": status_result,
        "controller_wait": controller_wait,
        "selector_restore": selector_restore,
        "ok": (
            restart_result["ok"]
            and status_result["ok"]
            and controller_wait["ok"]
            and (selector_restore is None or bool(selector_restore.get("ok")))
        ),
    }


def restart_mihomo_container(
    *,
    action: str = "restart",
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Restart Mihomo runtime with optional job heartbeat callback."""

    return _restart_mihomo_container(action=action, heartbeat=heartbeat)
