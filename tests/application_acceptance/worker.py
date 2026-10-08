"""Minimal real uvicorn worker used by the hosted process acceptance suite."""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
from pathlib import Path
from typing import Any


def _clean_environment() -> None:
    keep = {
        "PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
        "FWROUTER_STATE_DIR", "FWROUTER_ENVIRONMENT", "FWROUTER_STARTUP_TASKS_ENABLED",
        "FWROUTER_ACCEPTANCE_RPC_SOCKET", "FWROUTER_ACCEPTANCE_PROFILE",
        "FWROUTER_APPLICATION_ACCEPTANCE_ROOT", "FWROUTER_ACCEPTANCE_RECEIPT_PATH",
        "FWROUTER_XRAY_BINARY", "FWROUTER_MIHOMO_BINARY", "FWROUTER_BROWSER_EXECUTABLE",
        "FWROUTER_CHROMIUM_BINARY", "HOME", "TMPDIR",
    }
    clean = {key: os.environ[key] for key in keep if key in os.environ}
    os.environ.clear()
    os.environ.update(clean)
    os.environ["FWROUTER_ENVIRONMENT"] = "test"
    os.environ["FWROUTER_STARTUP_TASKS_ENABLED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def _rpc_call(socket_path: Path, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = json.dumps({"action": action, "payload": payload}, separators=(",", ":")).encode() + b"\n"
    if len(request) > 16 * 1024:
        raise ValueError("native runner request exceeds limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(9)
        client.connect(str(socket_path))
        client.sendall(request)
        data = bytearray()
        while len(data) <= 4 * 1024 * 1024:
            block = client.recv(65536)
            if not block:
                break
            data.extend(block)
            if b"\n" in data:
                break
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("native runner response exceeds limit")
    response = json.loads(bytes(data).split(b"\n", 1)[0])
    details = response.get("details") if isinstance(response.get("details"), dict) else {}
    if "archive_bytes_b64" in details:
        details["archive_bytes"] = base64.b64decode(details.pop("archive_bytes_b64"), validate=True)
    response["details"] = details
    return response


def build_app(socket_path: Path):
    state = Path(os.environ.get("FWROUTER_STATE_DIR", ""))
    root = Path(os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT", ""))
    if root != Path("/tmp/fwrouter-application-acceptance") or root.is_symlink() or not root.is_dir():
        raise RuntimeError("acceptance worker root is not the fixed orchestrator-owned /tmp root")
    state_real = state.resolve(strict=True)
    root_real = root.resolve(strict=True)
    if not state_real.is_relative_to(root_real) or state.is_symlink():
        raise RuntimeError("application state escapes the owned acceptance root")
    if socket_path.is_symlink() or not socket_path.is_socket() or socket_path.stat().st_uid != os.getuid() or socket_path.stat().st_mode & 0o077:
        raise RuntimeError("native runner socket is not an owned private AF_UNIX socket")
    socket_real = socket_path.resolve(strict=True)
    if not socket_real.is_relative_to(state_real):
        raise RuntimeError("native runner socket escapes the isolated application state")
    from .profile import ProfileError, load_profile
    try:
        load_profile(Path(os.environ.get("FWROUTER_ACCEPTANCE_PROFILE", "")))
    except (OSError, ProfileError) as exc:
        raise RuntimeError("acceptance worker profile qualification failed") from exc

    # Import settings before any facade module, and prevent BaseSettings from
    # consulting the deployed absolute dotenv path in a worker process.
    from fwrouter_api.core.config import Settings, get_settings
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()

    from fwrouter_api.adapters.xray import RealXrayAdapter
    from fwrouter_api.adapters.xray_common import XrayApplyResult

    config_path = state / "xray" / "config.json"
    compose_path = state / "xray" / "acceptance-compose.yml"

    def runner(action: str, payload: dict[str, Any]) -> XrayApplyResult:
        reply = _rpc_call(socket_path, action, payload)
        return XrayApplyResult(
            ok=bool(reply.get("ok")), message=str(reply.get("message") or "native Xray action"),
            error_code=reply.get("error_code"), details=reply.get("details") or {},
        )

    adapter = RealXrayAdapter(config_path=config_path, compose_path=compose_path, runner=runner)
    from fwrouter_api.adapters import xray as xray_adapter_module
    from fwrouter_api.services import xray as xray_service
    from fwrouter_api.services import subject_inventory, runtime, xray_runtime_state, xray_status

    xray_adapter_module.DEFAULT_XRAY_ADAPTER = adapter
    xray_service.DEFAULT_XRAY_ADAPTER = adapter
    for module in (subject_inventory, runtime, xray_runtime_state, xray_status):
        if hasattr(module, "DEFAULT_XRAY_ADAPTER"):
            module.DEFAULT_XRAY_ADAPTER = adapter

    from fwrouter_api.jobs.extended_handlers import register_extended_handlers
    from fwrouter_api.jobs.manager import get_default_job_manager
    from fwrouter_api.jobs.handlers import register_default_handlers
    register_default_handlers(get_default_job_manager())
    register_extended_handlers(get_default_job_manager())

    from fwrouter_api.main import create_app
    return create_app(enable_startup_tasks=False)


def main() -> None:
    _clean_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    socket_path = Path(args.socket)
    if str(socket_path) != os.environ.get("FWROUTER_ACCEPTANCE_RPC_SOCKET"):
        raise RuntimeError("worker RPC argument differs from the qualified environment")
    app = build_app(socket_path)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
