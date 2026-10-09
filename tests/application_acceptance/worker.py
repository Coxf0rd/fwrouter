"""Minimal real uvicorn worker used by the hosted process acceptance suite."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import sys
import threading
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


def _install_acceptance_mihomo_fence_observer() -> None:
    """Capture bounded hash-only evidence when the real publication fence refuses."""
    from fwrouter_api.services import mihomo_reconcile as reconcile

    original_fingerprint = reconcile.current_mihomo_input_fingerprint
    original_fence = reconcile._selection_publication_still_owned
    original_revision = reconcile.read_selection_revision
    original_incarnation = reconcile._mihomo_incarnation
    original_file_hash = reconcile._file_hash
    original_guard_held = reconcile.xray_writer_guard_is_held
    fingerprints: list[dict[str, Any]] = []
    state: dict[str, Any] = {"inside_fence": False, "reads": {}, "emitted": False}

    def leaf_hashes(value: Any, prefix: str = "", output: dict[str, str] | None = None) -> dict[str, str]:
        output = output if output is not None else {}
        if len(output) >= 1024:
            return output
        if isinstance(value, dict) and value:
            for key in sorted(value, key=str):
                leaf_hashes(value[key], f"{prefix}.{key}" if prefix else str(key), output)
        elif isinstance(value, list) and value:
            for index, item in enumerate(value):
                leaf_hashes(item, f"{prefix}[{index}]", output)
        else:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), default=str).encode("utf-8")
            output[prefix or "root"] = hashlib.sha256(encoded).hexdigest()
        return output

    def observed_fingerprint(routing=None):
        result = original_fingerprint(routing)
        if isinstance(result, dict) and isinstance(result.get("payload"), dict):
            row = {"hash": result.get("hash"), "leaves": leaf_hashes(result["payload"]),
                   "inside_fence": bool(state["inside_fence"])}
            fingerprints.append(row)
            if len(fingerprints) > 32:
                del fingerprints[:-32]
            if state["inside_fence"]:
                state["reads"]["input_fingerprint"] = row
        return result

    def observed_revision(connection):
        value = original_revision(connection)
        if state["inside_fence"]:
            state["reads"]["selection_revision"] = value
        return value

    def observed_incarnation():
        value = original_incarnation()
        if state["inside_fence"]:
            state["reads"]["runtime_incarnation"] = value
        return value

    def observed_file_hash(path):
        value = original_file_hash(path)
        if state["inside_fence"]:
            state["reads"]["active_config_hash"] = value
        return value

    def observed_guard_held():
        value = original_guard_held()
        if state["inside_fence"]:
            state["reads"]["writer_guard_held"] = bool(value)
        return value

    def observed_fence(**kwargs):
        state["reads"] = {}
        state["inside_fence"] = True
        try:
            owned = original_fence(**kwargs)
        finally:
            state["inside_fence"] = False
        if owned or state["emitted"]:
            return owned

        expected_input_hash = kwargs.get("expected_input_hash")
        expected = next((row for row in reversed(fingerprints)
                         if row["hash"] == expected_input_hash and not row["inside_fence"]), None)
        current = state["reads"].get("input_fingerprint")
        changed_paths = []
        changed_leaves = []
        if expected and current:
            before, after = expected["leaves"], current["leaves"]
            changed_paths = sorted(path for path in set(before) | set(after)
                                   if before.get(path) != after.get(path))
            changed_leaves = [{"path": path, "expected_sha256": before.get(path),
                               "observed_sha256": after.get(path)}
                              for path in changed_paths[:64]]
        revision = state["reads"].get("selection_revision")
        incarnation = state["reads"].get("runtime_incarnation")
        active_hash = state["reads"].get("active_config_hash")
        verification = kwargs.get("verification")
        checks = {
            "writer_guard_held": state["reads"].get("writer_guard_held"),
            "selection_revision_matches": revision == kwargs.get("expected_revision"),
            "runtime_incarnation_matches": incarnation == kwargs.get("expected_incarnation"),
            "input_fingerprint_matches": bool(current and current.get("hash") == expected_input_hash),
            "active_config_hash_matches": active_hash == kwargs.get("expected_active_hash"),
            "verification_matches": verification is None or bool(
                str(verification.get("operation_id") or "") == str(kwargs.get("operation_id") or "")
                and type(verification.get("selection_revision")) is int
                and verification.get("selection_revision") == kwargs.get("expected_revision")
            ),
        }
        record = {
            "schema": "fwrouter-acceptance-mihomo-publication-fence/v1",
            "result": "refused",
            "checks": checks,
            "expected": {
                "selection_revision": kwargs.get("expected_revision"),
                "runtime_incarnation_sha256": hashlib.sha256(str(kwargs.get("expected_incarnation") or "").encode()).hexdigest(),
                "input_fingerprint_sha256": expected_input_hash,
                "active_config_sha256": kwargs.get("expected_active_hash"),
            },
            "observed": {
                "selection_revision": revision,
                "runtime_incarnation_sha256": hashlib.sha256(str(incarnation or "").encode()).hexdigest(),
                "input_fingerprint_sha256": current.get("hash") if current else None,
                "active_config_sha256": active_hash,
            },
            "fingerprint_changed_leaves": changed_leaves,
            "fingerprint_changed_leaves_truncated": len(changed_paths) > len(changed_leaves),
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 12 * 1024:
            record["fingerprint_changed_leaves"] = changed_leaves[:24]
            record["fingerprint_changed_leaves_truncated"] = True
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_FENCE " + encoded + "\n")
        sys.stderr.flush()
        state["emitted"] = True
        return owned

    reconcile.current_mihomo_input_fingerprint = observed_fingerprint
    reconcile.read_selection_revision = observed_revision
    reconcile._mihomo_incarnation = observed_incarnation
    reconcile._file_hash = observed_file_hash
    reconcile.xray_writer_guard_is_held = observed_guard_held
    reconcile._selection_publication_still_owned = observed_fence


def _install_acceptance_provider_verification_observer() -> None:
    """Record bounded redacted phases for exceptions hidden by provider verification."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("provider verification observer requires the qualified acceptance worker")

    from .native_runner import _redact_diagnostic
    from fwrouter_api.services import provider_managed

    state = {"records": 0}
    record_guard = threading.Lock()

    def record_exception(phase: str, exc: Exception, *, line: int | None = None) -> None:
        with record_guard:
            if state["records"] >= 8:
                return
            state["records"] += 1
        record = {
            "schema": "fwrouter-acceptance-provider-verification-exception/v1",
            "phase": phase,
            "exception_type": type(exc).__name__[:96],
            "message": _redact_diagnostic(str(exc), limit=256),
        }
        if line is not None:
            record["line"] = int(line)
        try:
            sys.stderr.write("FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION "
                             + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def record_observer_installed() -> None:
        with record_guard:
            if state["records"] >= 8:
                return
            state["records"] += 1
        record = {
            "schema": "fwrouter-acceptance-provider-verification-exception/v1",
            "event": "observer_installed",
        }
        try:
            sys.stderr.write("FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION "
                             + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def record_verifier_entered() -> None:
        with record_guard:
            if state["records"] >= 8:
                return
            state["records"] += 1
        record = {
            "schema": "fwrouter-acceptance-provider-verification-exception/v1",
            "event": "verify_enter",
        }
        try:
            sys.stderr.write("FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION "
                             + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def safe_refresh_summary(value: Any, *, depth: int = 0) -> Any:
        allowed = {
            "ok", "stage", "outcome", "status", "runtime_verified", "intent_saved",
            "last_good_retained", "error_code", "code", "promoted", "container_restarted",
            "applied", "update_available", "reconcile_action", "timed_out", "retained",
            "error", "reconcile", "verification_callback_result", "refresh", "source_outcomes",
        }
        if depth >= 5:
            return None
        if isinstance(value, dict):
            summary: dict[str, Any] = {}
            for key, item in list(value.items())[:32]:
                if str(key) not in allowed:
                    continue
                if isinstance(item, dict):
                    nested = safe_refresh_summary(item, depth=depth + 1)
                    if nested:
                        summary[str(key)] = nested
                elif isinstance(item, list):
                    nested = [safe_refresh_summary(entry, depth=depth + 1) for entry in item[:8]]
                    summary[str(key)] = [entry for entry in nested if entry]
                elif isinstance(item, bool) or (isinstance(item, int) and not isinstance(item, bool)):
                    summary[str(key)] = item
                elif isinstance(item, str) and len(item) <= 80 and all(
                    char.isascii() and (char.isalnum() or char in "_.-") for char in item
                ):
                    summary[str(key)] = item
            return summary
        if isinstance(value, bool) or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        if isinstance(value, str) and len(value) <= 80 and all(
            char.isascii() and (char.isalnum() or char in "_.-") for char in value
        ):
            return value
        return None

    def record_refresh_result(binding: Any, result: Any) -> None:
        with record_guard:
            if state["records"] >= 8:
                return
            state["records"] += 1
        expected_ref = binding.get("source_ref") if isinstance(binding, dict) else None
        refresh = result.get("refresh") if isinstance(result, dict) else None
        batch = refresh.get("batch") if isinstance(refresh, dict) else None
        targeted_ref = batch.get("targeted_source_ref") if isinstance(batch, dict) else None
        record = {
            "schema": "fwrouter-acceptance-provider-verification-exception/v1",
            "event": "refresh_result",
            "targeted_source_match": bool(expected_ref and targeted_ref and expected_ref == targeted_ref),
            "summary": safe_refresh_summary(result),
        }
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) > 2048:
                record["summary"] = {"truncated": True}
                encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            sys.stderr.write("FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION " + encoded + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    original_verify = provider_managed.verify_provider_handoff
    original_refresh = provider_managed._refresh_with_material

    def observed_refresh_with_material(binding: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_refresh(binding, *args, **kwargs)
        record_refresh_result(binding, result)
        return result

    provider_managed._refresh_with_material = observed_refresh_with_material

    def observe_verify_exception(frame: Any, event: str, arg: Any) -> None:
        if event == "exception" and frame.f_code is original_verify.__code__:
            _exc_type, exc, _traceback = arg
            record_exception("provider_managed.verify_provider_handoff", exc, line=frame.f_lineno)

    def scoped_trace(frame: Any, event: str, arg: Any) -> Any:
        observe_verify_exception(frame, event, arg)
        return scoped_trace

    def observed_verify_provider_handoff(*args: Any, **kwargs: Any) -> dict[str, Any]:
        record_verifier_entered()
        previous_trace = sys.gettrace()
        sys.settrace(scoped_trace)
        try:
            return original_verify(*args, **kwargs)
        finally:
            sys.settrace(previous_trace)

    provider_managed.verify_provider_handoff = observed_verify_provider_handoff
    record_observer_installed()


def _install_acceptance_generation_callback_observer() -> None:
    """Capture bounded exception provenance at the staged-generation callback seam."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("generation callback observer requires the qualified acceptance worker")

    from .native_runner import _redact_diagnostic
    from fwrouter_api.services import mihomo_reconcile

    state = {"records": 0}
    lock = threading.Lock()
    original_invoke = mihomo_reconcile._invoke_verification_callback

    def record(record: dict[str, Any]) -> None:
        with lock:
            if state["records"] >= 8:
                return
            state["records"] += 1
        record = {
            "schema": "fwrouter-acceptance-generation-callback/v1",
            **record,
        }
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) > 2048:
                return
            sys.stderr.write("FWROUTER_ACCEPTANCE_GENERATION_CALLBACK " + encoded + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def observed_invoke(callback: Any, *, operation_id: str, expected_revision: int) -> Any:
        callback_code = getattr(callback, "__code__", None)
        callback_name = callback_code.co_name if callback_code is not None else type(callback).__name__
        record({"event": "callback_enter", "callback": str(callback_name)[:96]})
        try:
            result = original_invoke(
                callback, operation_id=operation_id, expected_revision=expected_revision,
            )
        except Exception as exc:
            frames: list[dict[str, Any]] = []
            current = exc.__traceback__
            while current is not None:
                code = current.tb_frame.f_code
                frames.append({
                    "function": code.co_name[:96],
                    "file": Path(code.co_filename).name[:96],
                    "line": int(current.tb_lineno),
                })
                current = current.tb_next
            record({
                "event": "callback_exception",
                "callback": str(callback_name)[:96],
                "exception_type": type(exc).__name__[:96],
                "message": _redact_diagnostic(str(exc), limit=256),
                "frames": frames[-3:],
            })
            raise
        summary = {"event": "callback_result", "callback": str(callback_name)[:96]}
        if isinstance(result, dict):
            summary["ok"] = bool(result.get("ok"))
            for key in ("status", "stage", "error_code"):
                value = result.get(key)
                if isinstance(value, str) and len(value) <= 96 and all(
                    char.isascii() and (char.isalnum() or char in "_.- ") for char in value
                ):
                    summary[key] = value
            selector = result.get("selector")
            if isinstance(selector, dict):
                selector_summary: dict[str, Any] = {}
                for key in (
                    "error_code", "selection_outcome", "status", "selection_basis", "reason",
                    "applied", "candidates_count", "selection_revision", "runtime_adapter_id",
                    "checked_count", "success_count", "failed_count",
                ):
                    value = selector.get(key)
                    if isinstance(value, bool) or (isinstance(value, int) and not isinstance(value, bool)):
                        selector_summary[key] = value
                    elif isinstance(value, str) and len(value) <= 96 and all(
                        char.isascii() and (char.isalnum() or char in "_.- ") for char in value
                    ):
                        selector_summary[key] = value
                on_demand = selector.get("on_demand")
                if isinstance(on_demand, dict):
                    on_demand_summary = {
                        key: int(on_demand[key]) for key in ("checked_count", "success_count", "failed_count")
                        if type(on_demand.get(key)) is int
                    }
                    rows = on_demand.get("results")
                    if isinstance(rows, list):
                        safe_rows: list[dict[str, Any]] = []
                        for row in rows[:4]:
                            if not isinstance(row, dict):
                                continue
                            safe_row: dict[str, Any] = {}
                            for key in ("status", "error_code"):
                                value = row.get(key)
                                if isinstance(value, str) and len(value) <= 96 and all(
                                    char.isascii() and (char.isalnum() or char in "_.- ") for char in value
                                ):
                                    safe_row[key] = value
                            message = row.get("error_message")
                            if isinstance(message, str) and message:
                                safe_row["error_message"] = _redact_diagnostic(message, limit=256)
                            safe_rows.append(safe_row)
                        on_demand_summary["results"] = safe_rows
                    selector_summary["on_demand"] = on_demand_summary
                provider_operation = selector.get("provider_operation")
                if isinstance(provider_operation, dict):
                    selector_summary["provider_operation"] = {
                        key: provider_operation[key] for key in ("error_code", "outcome", "runtime_verified")
                        if isinstance(provider_operation.get(key), (str, bool))
                        and (not isinstance(provider_operation.get(key), str)
                             or len(provider_operation[key]) <= 96 and all(
                                 char.isascii() and (char.isalnum() or char in "_.- ")
                                 for char in provider_operation[key]
                             ))
                    }
                summary["selector"] = selector_summary
        else:
            summary["ok"] = bool(result)
        record(summary)
        return result

    mihomo_reconcile._invoke_verification_callback = observed_invoke


def _install_acceptance_mihomo_delay_error_observer() -> None:
    """Capture bounded Mihomo delay error bodies without changing HTTP behavior."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("Mihomo delay observer requires the qualified acceptance worker")

    import re
    import httpx
    from .native_runner import _redact_diagnostic

    original_raise = httpx.Response.raise_for_status
    state = {"records": 0}
    lock = threading.Lock()
    url_pattern = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
    endpoint_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")

    def response_error(response: Any) -> None:
        try:
            request_url = response.request.url
            if request_url.host != "127.0.0.1" or request_url.port != 5200:
                return
            path = str(request_url.path)
            if path.startswith("/group/") and path.endswith("/delay"):
                category = "group_delay"
            elif path.startswith("/proxies/") and path.endswith("/delay"):
                category = "proxy_delay"
            else:
                return
            status = int(response.status_code)
            if status < 400 or status > 599:
                return
            payload = json.loads(bytes(response.content[:8192]).decode("utf-8", "replace"))
            message: Any = None
            if isinstance(payload, dict):
                message = payload.get("message")
                if not isinstance(message, str):
                    error = payload.get("error")
                    message = error.get("message") if isinstance(error, dict) else error
            if not isinstance(message, str):
                return
            safe_message = _redact_diagnostic(message, limit=512)
            safe_message = endpoint_pattern.sub("[ENDPOINT]", url_pattern.sub("[URL]", safe_message))
            safe_message = _redact_diagnostic(safe_message, limit=256)
            with lock:
                if state["records"] >= 8:
                    return
                state["records"] += 1
            record = {
                "schema": "fwrouter-acceptance-mihomo-delay-error/v1",
                "status": status,
                "path_category": category,
                "error_message": safe_message,
            }
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) > 512:
                return
            sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_DELAY_ERROR " + encoded + "\n")
            sys.stderr.flush()
        except Exception:
            return

    def observed_raise_for_status(response: Any) -> Any:
        response_error(response)
        return original_raise(response)

    httpx.Response.raise_for_status = observed_raise_for_status


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
    _install_acceptance_mihomo_fence_observer()
    _install_acceptance_provider_verification_observer()
    _install_acceptance_generation_callback_observer()
    _install_acceptance_mihomo_delay_error_observer()
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
    from fwrouter_api.services.mihomo_config_paths import _resolved_candidate_config_path

    expected_candidate = (state / "generated" / "mihomo" / "config.next.yaml").resolve(strict=False)
    default_candidate = Path(_resolved_candidate_config_path()).resolve(strict=False)
    if default_candidate != expected_candidate:
        raise RuntimeError("canonical Mihomo candidate path escapes the owned acceptance state")
    active_config = state / "generated" / "mihomo" / "config.yaml"
    active_contours = state / "generated" / "mihomo" / "contours.json"
    provider_endpoint = os.environ.get("FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL", "").strip()
    parsed_provider_endpoint = urlsplit(provider_endpoint)
    if (parsed_provider_endpoint.scheme != "http" or parsed_provider_endpoint.hostname != "127.0.0.1"
            or parsed_provider_endpoint.port is None or parsed_provider_endpoint.path not in {"", "/"}
            or parsed_provider_endpoint.username or parsed_provider_endpoint.password
            or parsed_provider_endpoint.query or parsed_provider_endpoint.fragment):
        raise RuntimeError("acceptance Mihomo probe target must be the owned loopback provider bridge")

    member_diagnostic_state = {"used": False}
    member_diagnostic_lock = threading.Lock()

    def record_failed_member_delay(adapter: Any, logical_runtime_target: str, test_url: str) -> None:
        """Make at most one read-only loopback member delay request after a group failure."""
        with member_diagnostic_lock:
            if member_diagnostic_state["used"]:
                return
            member_diagnostic_state["used"] = True
        from urllib.parse import quote, urlsplit
        import re
        import httpx
        from .native_runner import _redact_diagnostic

        record: dict[str, Any] = {
            "schema": "fwrouter-acceptance-mihomo-member-delay-diagnostic/v1",
            "event": "attempt",
            "path_category": "proxy_delay",
        }
        try:
            controller = urlsplit(str(adapter.base_url))
            if (controller.scheme != "http" or controller.hostname != "127.0.0.1"
                    or controller.port != 5200 or controller.username or controller.password
                    or controller.path not in {"", "/"} or controller.query or controller.fragment):
                record["result"] = "controller_not_owned_loopback"
                return
            snapshot = adapter.get_logical_group_state(logical_runtime_target)
            members = snapshot.get("members") if isinstance(snapshot, dict) else None
            first = next((item for item in members or []
                          if isinstance(item, dict) and isinstance(item.get("runtime_identity"), str)
                          and item["runtime_identity"]), None)
            if first is None:
                record["result"] = "member_unavailable"
                record["member_available"] = False
                return
            record["member_available"] = True
            member = str(first["runtime_identity"])
            url = f"{adapter.base_url}/proxies/{quote(member, safe='')}/delay"
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                response = client.get(
                    url,
                    headers=adapter._headers(),
                    params={"timeout": 1000, "url": test_url},
                )
            record["status"] = int(response.status_code)
            body: Any = None
            try:
                body = json.loads(bytes(response.content[:8192]).decode("utf-8", "replace"))
            except (TypeError, ValueError):
                pass
            message = None
            code = None
            if isinstance(body, dict):
                message = body.get("message")
                error = body.get("error")
                if not isinstance(message, str):
                    message = error.get("message") if isinstance(error, dict) else error
                candidate_code = body.get("error_code")
                if isinstance(candidate_code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate_code):
                    code = candidate_code
            record["body_flags"] = {
                "json_object": isinstance(body, dict),
                "delay_present": isinstance(body, dict) and isinstance(body.get("delay"), int),
                "error_code_present": code is not None,
                "error_message_present": isinstance(message, str) and bool(message),
            }
            if code:
                record["error_code"] = code
            if isinstance(message, str) and message:
                safe = _redact_diagnostic(message, limit=512)
                safe = re.sub(r"(?i)\bhttps?://[^\s\"'<>]+", "[URL]", safe)
                safe = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b", "[ENDPOINT]", safe)
                safe = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[ADDRESS]", safe)
                safe = re.sub(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d{1,5})?\b", "[HOST]", safe)
                record["error_message"] = _redact_diagnostic(safe, limit=256)
            record["result"] = "response_received"
        except Exception as exc:
            record["result"] = "request_failed"
            record["exception_type"] = type(exc).__name__[:80]
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) <= 1024:
                sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_MEMBER_DELAY " + encoded + "\n")
                sys.stderr.flush()
        except Exception:
            pass

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
            failed = next((item for item in result if isinstance(item, dict) and item.get("ok") is False), None)
            if failed is not None and logical_runtime_targets:
                try:
                    record_failed_member_delay(
                        self, str(failed.get("logical_runtime_target") or logical_runtime_targets[0]),
                        self._acceptance_probe_url(test_url),
                    )
                except Exception:
                    pass
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
        path = str(candidate_path or default_candidate)
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
