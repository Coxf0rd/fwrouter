from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable
from typing import Any

from fwrouter_api.adapters.mihomo import DEFAULT_MIHOMO_ADAPTER, MihomoRuntimeState
from fwrouter_api.services.modules import managed_runtime_operation_blocked


MIHOMO_COMPOSE_FILE = Path("/opt/fwrouter-mihomo/docker-compose.yml")
MIHOMO_COMPOSE_SERVICE = "mihomo"
DOCKER_CLI_STATE_DIR = Path("/run/fwrouter-v2/docker-cli")


def _run_compose_command(args: list[str], *, timeout_seconds: int = 30) -> dict[str, Any]:
    """Run docker compose command for the Mihomo runtime project."""

    command = [
        "docker",
        "compose",
        "-f",
        str(MIHOMO_COMPOSE_FILE),
        *args,
    ]

    DOCKER_CLI_STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "DOCKER_CONFIG": str(DOCKER_CLI_STATE_DIR),
        "HOME": str(DOCKER_CLI_STATE_DIR),
    }

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=env,
    )

    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "ok": result.returncode == 0,
    }


def get_mihomo_runtime_incarnation(*, timeout_seconds: float | None = None) -> str | None:
    """Read the container ID and StartedAt value as a restart fence."""
    if os.environ.get("FWROUTER_ENVIRONMENT", "production").strip().lower() == "test":
        return None
    deadline = time.monotonic() + max(0.1, float(timeout_seconds)) if timeout_seconds is not None else None
    listed = _run_compose_command(["ps", "-q", MIHOMO_COMPOSE_SERVICE],
                                  timeout_seconds=max(0.1, deadline - time.monotonic()) if deadline else 5)
    container_id = str(listed.get("stdout") or "").strip().splitlines()
    if not listed.get("ok") or not container_id:
        return None
    if deadline is not None and deadline <= time.monotonic():
        return None
    state_dir = DOCKER_CLI_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DOCKER_CONFIG": str(state_dir), "HOME": str(state_dir)}
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}|{{.State.StartedAt}}", container_id[-1]],
            check=False,
            capture_output=True,
            text=True,
            timeout=(max(0.1, deadline - time.monotonic()) if deadline else 5),
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = str(inspected.stdout or "").strip()
    return value if inspected.returncode == 0 and value and "|" in value else None


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
    selection_fenced: bool = False,
    expected_selection_revision: int | None = None,
) -> dict[str, Any]:
    """Restart Mihomo runtime with optional job heartbeat callback."""

    blocked = managed_runtime_operation_blocked(
        "vpn",
        error_code="MIHOMO_MANAGED_RUNTIME_REQUIRED",
        operation=f"mihomo_container_{action}",
    )
    if blocked is not None:
        return {
            **blocked,
            "compose_file": str(MIHOMO_COMPOSE_FILE),
            "service": MIHOMO_COMPOSE_SERVICE,
            "action": action,
        }

    if selection_fenced:
        from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held
        from fwrouter_api.db.connection import db_session
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
        if not xray_writer_guard_is_held() or type(expected_selection_revision) is not int:
            return {"ok": False, "action": action, "error_code": "VPN_AUTO_SELECTION_FENCE_REQUIRED"}
        with db_session() as connection:
            if read_selection_revision(connection) != expected_selection_revision:
                return {"ok": False, "action": action, "error_code": "VPN_AUTO_SELECTION_REVISION_CONFLICT"}
        return _restart_mihomo_container(action=action, heartbeat=heartbeat)

    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_revision

    with xray_writer_guard(timeout_seconds=30.0):
        with db_session() as connection:
            revision = read_selection_revision(connection)
            next_revision = advance_selection_revision(connection, expected_revision=revision)
        if next_revision is None:
            return {
                "ok": False, "action": action,
                "error_code": "VPN_AUTO_SELECTION_REVISION_CONFLICT",
            }
        result = _restart_mihomo_container(action=action, heartbeat=heartbeat)
        return {**result, "selection_revision": next_revision}
