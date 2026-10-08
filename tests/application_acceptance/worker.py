"""Minimal real uvicorn worker used by the hosted process acceptance suite."""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any


def _clean_environment() -> None:
    keep = {
        "PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
        "FWROUTER_STATE_DIR", "FWROUTER_ENVIRONMENT", "FWROUTER_STARTUP_TASKS_ENABLED",
        "FWROUTER_ACCEPTANCE_RPC_SOCKET", "FWROUTER_ACCEPTANCE_PROFILE",
        "FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL",
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
        client.settimeout(25)
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
    if "config_bytes_b64" in details:
        details["config_bytes"] = base64.b64decode(details.pop("config_bytes_b64"), validate=True)
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
    app = create_app(enable_startup_tasks=False)
    _bind_xray_checkpoint_barrier(socket_path, state)
    mihomo_controller = _bind_acceptance_mihomo(socket_path, state)
    _bind_acceptance_provider()

    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("acceptance controller routes require the qualified test worker")

    # Observe the original Core CAS only after its caller has completed native
    # apply and selector readback. The native parent may hold this transport
    # boundary while a test advances the real persistent revision.
    from fwrouter_api.services import selector as selector_service
    _original_commit_active_selection = selector_service.commit_active_selection

    def commit_active_selection_with_acceptance_barrier(connection: Any, **kwargs: Any) -> int | None:
        provenance = kwargs.get("provenance") if isinstance(kwargs.get("provenance"), dict) else {}
        reply = _rpc_call(socket_path, "selection_commit_barrier", {
            "expected_revision": kwargs.get("expected_revision"),
            "expected_active_server_id": kwargs.get("expected_active_server_id"),
            "expected_provenance_decision_id": kwargs.get("expected_provenance_decision_id"),
            "server_id": kwargs.get("server_id"),
            "operation_id": str(provenance.get("operation_id") or ""),
        })
        if not reply.get("ok"):
            raise RuntimeError("acceptance selection commit barrier transport failed")
        return _original_commit_active_selection(connection, **kwargs)

    selector_service.commit_active_selection = commit_active_selection_with_acceptance_barrier

    def recover_xray_generation(payload: dict[str, Any]) -> dict[str, Any]:
        from fwrouter_api.services.xray_subscription_service import reconcile_xray_subscription_profile_nodes
        options: dict[str, Any] = {}
        if payload.get("verify_selection") is True:
            from fwrouter_api.services.xray_subscription_service import (
                _capture_generation_auto_selection,
                _verify_generation_selection_readback,
            )
            captured = _capture_generation_auto_selection()

            def verify_selection(**context: Any) -> dict[str, Any]:
                # The recovery contract must prove the stored Core fence, not
                # echo caller-supplied values as if they were a successful CAS.
                checkpoint = state / "xray" / ".generation" / "generation-checkpoint.json"
                durable = json.loads(checkpoint.read_text(encoding="utf-8"))
                operation_id = str(durable.get("selection_operation_id") or "")
                expected_revision = durable.get("selection_revision")
                from fwrouter_api.db.connection import db_session
                from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
                with db_session() as connection:
                    actual_fence = read_selection_fence(connection)
                result = _verify_generation_selection_readback(captured)
                context_matches = not context.get("operation_id") or context["operation_id"] == operation_id
                fence_matches = (
                    type(expected_revision) is int
                    and actual_fence["revision"] == expected_revision
                    and captured.get("selection_revision") == expected_revision
                )
                return {
                    **result,
                    "ok": bool(result.get("ok") and fence_matches and context_matches and operation_id),
                    "error_code": result.get("error_code") or (None if fence_matches else "SELECTION_FENCE_CHANGED"),
                    "operation_id": operation_id or None,
                    "selection_revision": actual_fence["revision"],
                    "expected_selection_revision": expected_revision,
                    "selection_fence": actual_fence,
                    "context_operation_id_matches": context_matches,
                }

            options["verification_callback"] = verify_selection
        return reconcile_xray_subscription_profile_nodes(
            requested_by="hosted-acceptance-recovery",
            token_or_slug=str(payload.get("token") or "") or None,
            materialize=True,
            **options,
        )

    app.add_api_route("/api/v2/__acceptance/xray/reconcile", recover_xray_generation, methods=["POST"])

    def wait_for_acceptance_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = payload.get("timeout_seconds", 75)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 90:
            return {"ok": False, "error_code": "ACCEPTANCE_JOB_WAIT_TIMEOUT_INVALID"}
        from fwrouter_api.jobs.manager import get_default_job_manager
        job = get_default_job_manager().wait_for_job(job_id, timeout_seconds=timeout_seconds)
        if job is None:
            return {"ok": False, "error_code": "ACCEPTANCE_JOB_NOT_FOUND"}
        return {"ok": job.get("status") in {"success", "failed", "cancelled"}, "job": job}

    app.add_api_route("/api/v2/__acceptance/jobs/{job_id}/wait", wait_for_acceptance_job, methods=["POST"])

    def advance_selection_revision_for_acceptance(payload: dict[str, Any]) -> dict[str, Any]:
        expected_revision = payload.get("expected_revision")
        if type(expected_revision) is not int or expected_revision < 0:
            return {"ok": False, "error_code": "ACCEPTANCE_SELECTION_REVISION_REQUEST_INVALID"}
        from fwrouter_api.db.connection import db_session
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_fence
        with db_session() as connection:
            before = read_selection_fence(connection)
            if before["revision"] != expected_revision:
                return {"ok": False, "error_code": "ACCEPTANCE_SELECTION_REVISION_STALE", "fence": before}
            revision = advance_selection_revision(connection, expected_revision=expected_revision)
            if revision is None:
                return {"ok": False, "error_code": "ACCEPTANCE_SELECTION_REVISION_STALE"}
            after = read_selection_fence(connection)
        return {"ok": True, "before": before, "fence": after, "revision": revision}

    app.add_api_route("/api/v2/__acceptance/selection/revision/advance", advance_selection_revision_for_acceptance, methods=["POST"])

    def read_selection_fence_for_acceptance() -> dict[str, Any]:
        from fwrouter_api.db.connection import db_session
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        with db_session() as connection:
            return {"ok": True, "fence": read_selection_fence(connection)}

    app.add_api_route("/api/v2/__acceptance/selection/fence", read_selection_fence_for_acceptance, methods=["GET"])

    def recover_provider_path(payload: dict[str, Any]) -> dict[str, Any]:
        from fwrouter_api.services.provider_recovery import confirmed_provider_recovery
        logical_server_id = payload.get("logical_server_id")
        decision_id = payload.get("decision_id")
        timeout_ms = payload.get("timeout_ms", 1000)
        allow_switch = payload.get("allow_switch")
        if (not isinstance(logical_server_id, str) or not logical_server_id.strip() or len(logical_server_id) > 128
                or not isinstance(decision_id, str) or not decision_id.strip() or len(decision_id) > 128
                or type(timeout_ms) is not int or not 100 <= timeout_ms <= 5000
                or type(allow_switch) is not bool):
            return {"ok": False, "status": "rejected", "error_code": "ACCEPTANCE_RECOVERY_REQUEST_INVALID"}
        return confirmed_provider_recovery(
            logical_server_id=logical_server_id.strip(), path_key="acceptance",
            decision_id=decision_id.strip(), controller=mihomo_controller,
            timeout_ms=timeout_ms, allow_switch=allow_switch,
        ) or {"ok": False, "status": "failed", "error_code": "PROVIDER_RECOVERY_NO_RESULT"}

    app.add_api_route("/api/v2/__acceptance/provider/recovery", recover_provider_path, methods=["POST"])

    def reenter_provider_path(payload: dict[str, Any]) -> dict[str, Any]:
        from fwrouter_api.services.provider_recovery import try_verified_reentry
        timeout_ms = payload.get("timeout_ms", 1000)
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 5000:
            return {"ok": False, "status": "rejected", "error_code": "ACCEPTANCE_REENTRY_REQUEST_INVALID"}
        return try_verified_reentry(mihomo_controller, timeout_ms=timeout_ms, reason="acceptance_reentry")

    app.add_api_route("/api/v2/__acceptance/provider/reentry", reenter_provider_path, methods=["POST"])
    return app


def _bind_xray_checkpoint_barrier(socket_path: Path, state: Path) -> None:
    """Expose a barrier after the checkpoint directory fsync has completed."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("Xray checkpoint barriers require the qualified test worker")
    from fwrouter_api.services import xray_subscription_service

    real_fsync_directory = xray_subscription_service._fsync_directory
    checkpoint_path = state / "xray" / ".generation" / "generation-checkpoint.json"

    def fsync_directory_with_checkpoint_barrier(path: Path) -> None:
        real_fsync_directory(path)
        try:
            directory = Path(path).resolve(strict=True)
            if directory != checkpoint_path.parent.resolve(strict=False):
                return
            value = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            phase = str(value.get("phase") or "") if isinstance(value, dict) else ""
        except (OSError, ValueError, TypeError):
            return
        if phase:
            _rpc_call(socket_path, "generation_checkpoint", {"phase": phase})

    xray_subscription_service._fsync_directory = fsync_directory_with_checkpoint_barrier


def _bind_acceptance_mihomo(socket_path: Path, state: Path):
    """Bind the real Mihomo HTTP adapter and process boundary to owned children."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("acceptance Mihomo process binding is available only in the qualified test worker")
    from fwrouter_api.adapters import mihomo as adapter_module
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from fwrouter_api.services import mihomo_runtime, subscription_pipeline

    active_config = state / "generated" / "mihomo" / "config.yaml"
    active_contours = state / "generated" / "mihomo" / "contours.json"
    provider_endpoint = os.environ.get("FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL", "").strip()
    parsed_provider_endpoint = urlsplit(provider_endpoint)
    if (parsed_provider_endpoint.scheme != "http" or parsed_provider_endpoint.hostname != "127.0.0.1"
            or parsed_provider_endpoint.port is None or parsed_provider_endpoint.path not in {"", "/"}
            or parsed_provider_endpoint.username or parsed_provider_endpoint.password
            or parsed_provider_endpoint.query or parsed_provider_endpoint.fragment):
        raise RuntimeError("acceptance Mihomo probe target must be the owned loopback provider bridge")

    class LoopbackProbeMihomoHttpAdapter(MihomoHttpAdapter):
        def _acceptance_probe_url(self, test_url: str) -> str:
            return f"{provider_endpoint}/generate_204"

        def _probe_result_barrier(self, target: str, result: Any) -> None:
            reply = _rpc_call(socket_path, "mihomo_probe_result", {
                "logical_runtime_target": str(target), "probe_ok": bool(result.get("ok")) if isinstance(result, dict) else bool(getattr(result, "ok", False)),
            })
            if not reply.get("ok"):
                raise RuntimeError("acceptance native probe-result barrier failed")

        def probe_logical_group(self, logical_runtime_target: str, *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> dict[str, Any]:
            result = super().probe_logical_group(
                logical_runtime_target, test_url=self._acceptance_probe_url(test_url), timeout_ms=timeout_ms,
            )
            self._probe_result_barrier(logical_runtime_target, result)
            return result

        def probe_logical_member(self, logical_runtime_target: str, member_runtime_identity: str, *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> dict[str, Any]:
            result = super().probe_logical_member(
                logical_runtime_target, member_runtime_identity, test_url=self._acceptance_probe_url(test_url), timeout_ms=timeout_ms,
            )
            self._probe_result_barrier(logical_runtime_target, result)
            return result

        def probe_logical_groups(self, logical_runtime_targets: list[str], *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> list[dict[str, Any]]:
            result = super().probe_logical_groups(
                logical_runtime_targets, test_url=self._acceptance_probe_url(test_url), timeout_ms=timeout_ms,
            )
            for target, observed in zip(logical_runtime_targets, result):
                self._probe_result_barrier(target, observed)
            return result

        def check_delay(self, server_id: str, *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000):
            result = super().check_delay(server_id, test_url=self._acceptance_probe_url(test_url), timeout_ms=timeout_ms)
            self._probe_result_barrier(server_id, result)
            return result

    active_adapter = LoopbackProbeMihomoHttpAdapter(
        base_url="http://127.0.0.1:5200", config_path=active_config,
        contours_path=active_contours, timeout_seconds=3.0,
    )
    previous_adapter = adapter_module.DEFAULT_MIHOMO_ADAPTER
    adapter_module.DEFAULT_MIHOMO_ADAPTER = active_adapter
    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, "DEFAULT_MIHOMO_ADAPTER", None) is previous_adapter:
            setattr(module, "DEFAULT_MIHOMO_ADAPTER", active_adapter)

    def process_command(args: list[str], *, timeout_seconds: int = 30) -> dict[str, Any]:
        if args == ["ps", "-q", "mihomo"]:
            reply = _rpc_call(socket_path, "mihomo_incarnation", {})
            incarnation = str((reply.get("details") or {}).get("stdout") or "")
            return {"command": ["acceptance-mihomo", *args], "returncode": 0 if reply.get("ok") and incarnation else 1,
                    "stdout": incarnation.split("|", 1)[0] if incarnation else "", "stderr": "", "ok": bool(reply.get("ok") and incarnation)}
        if args == ["ps", "mihomo"]:
            reply = _rpc_call(socket_path, "mihomo_status", {})
            running = bool(reply.get("ok"))
            return {"command": ["acceptance-mihomo", *args], "returncode": 0 if running else 1,
                    "stdout": "mihomo running" if running else "mihomo stopped", "stderr": "", "ok": running}
        if args in (["restart", "mihomo"], ["up", "-d", "--force-recreate", "mihomo"]):
            reply = _rpc_call(socket_path, "mihomo_restart", {"config_path": str(active_config)})
            return {"command": ["acceptance-mihomo", *args], "returncode": 0 if reply.get("ok") else 1,
                    "stdout": str(reply.get("message") or ""), "stderr": str(reply.get("error_code") or ""), "ok": bool(reply.get("ok"))}
        raise RuntimeError("unsupported Mihomo process command in acceptance worker")

    def process_incarnation(*, timeout_seconds: float | None = None) -> str | None:
        reply = _rpc_call(socket_path, "mihomo_incarnation", {})
        value = str((reply.get("details") or {}).get("stdout") or "")
        return value if reply.get("ok") and "|" in value else None

    # These replace only the external Docker process transport. Config generation,
    # validation, controller HTTP calls, selection and reconciliation stay real.
    mihomo_runtime._run_compose_command = process_command
    mihomo_runtime.get_mihomo_runtime_incarnation = process_incarnation

    def validate_candidate(candidate_path: str | None = None, *, image_reference: str | None = None) -> dict[str, Any]:
        path = str(candidate_path or "")
        reply = _rpc_call(socket_path, "mihomo_test_config", {"path": path})
        details = reply.get("details") if isinstance(reply.get("details"), dict) else {}
        return {"ok": bool(reply.get("ok")), "returncode": 0 if reply.get("ok") else 1,
                "stdout_tail": str(details.get("stdout") or "")[-1000:],
                "stderr_tail": str(details.get("stderr") or "")[-1000:],
                "error_code": reply.get("error_code")}

    # Replace only the Docker validator transport with the pinned real Mihomo
    # binary's `-t`; candidate generation and validation outcome stay genuine.
    subscription_pipeline.validate_mihomo_candidate_config = validate_candidate
    return active_adapter


def _bind_acceptance_provider() -> None:
    """Point the genuine provider client at the owned loopback HTTP source."""
    endpoint = os.environ.get("FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL", "").strip()
    if not endpoint:
        return
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("acceptance provider transport is available only in the qualified test worker")
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("acceptance provider endpoint has an invalid port") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise RuntimeError("acceptance provider endpoint must be plain HTTP on loopback without a path")
    from fwrouter_api.adapters import stealthsurf

    base_client = stealthsurf.StealthSurfClient

    class LoopbackAcceptanceStealthSurfClient(base_client):
        def __init__(self, api_key: str, **kwargs: Any) -> None:
            kwargs["base_url"] = endpoint
            super().__init__(api_key, **kwargs)

    stealthsurf.StealthSurfClient = LoopbackAcceptanceStealthSurfClient



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
