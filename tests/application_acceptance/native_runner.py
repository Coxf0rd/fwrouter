"""Parent-owned Xray process and bounded AF_UNIX RPC transport for app workers."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_REQUEST = 16 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
RPC_ACTIONS = {"test_config", "reload", "compose_ps", "api_inbound_users", "runtime_container_id",
               "runtime_inspect", "runtime_config_archive", "mihomo_test_config", "mihomo_config_snapshot", "mihomo_probe_result", "mihomo_restart",
               "mihomo_status", "mihomo_incarnation", "generation_checkpoint", "selection_commit_barrier"}
DIAGNOSTIC_STREAM_LIMIT = 16 * 1024
DIAGNOSTIC_COMMAND_LIMIT = 64
DIAGNOSTIC_TOTAL_LIMIT = 256 * 1024
_SECRET_VALUE = re.compile(
    r"(?i)([\"']?\b(?:password|passwd|secret|token|authorization|api[_-]?key|"
    r"private[_-]?key|pre[_-]?shared[_-]?key|client[_-]?secret|credential)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_PEM_BLOCK = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)
_UUID_VALUE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b")
_EMAIL_VALUE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)


def _redact_diagnostic(value: bytes | str, *, limit: int = DIAGNOSTIC_STREAM_LIMIT) -> str:
    if limit < 1:
        return ""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    text = _SECRET_VALUE.sub(r"\1[REDACTED]", text)
    text = _BEARER_VALUE.sub(r"\1[REDACTED]", text)
    text = _PEM_BLOCK.sub("[REDACTED PEM]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = _UUID_VALUE.sub("[UUID]", text)
    text = _EMAIL_VALUE.sub("[EMAIL]", text)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) > limit:
        marker = b"[truncated]\n"
        encoded = marker[:limit] if limit <= len(marker) else marker + encoded[-(limit - len(marker)):]
    return encoded.decode("utf-8", "replace")


class _BoundedPipe:
    """Continuously drain a child pipe while retaining only its bounded tail."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._tail = bytearray()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while True:
            block = self._stream.read1(8192)
            if not block:
                return
            with self._lock:
                self._tail.extend(block)
                if len(self._tail) > DIAGNOSTIC_STREAM_LIMIT:
                    del self._tail[:-DIAGNOSTIC_STREAM_LIMIT]

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._tail)

    def close(self) -> None:
        # Callers reap/kill the process group first. Do not close a descriptor
        # under a live reader thread: BufferedReader.close() can block on its lock.
        self._thread.join(timeout=2)
        if not self._thread.is_alive():
            try:
                self._stream.close()
            except OSError:
                pass


def _bounded_child(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float,
                   stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess[bytes]:
    child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
    assert child.stdout is not None and child.stderr is not None
    stdout, stderr = _BoundedPipe(child.stdout), _BoundedPipe(child.stderr)
    try:
        code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=3)
        code = 124
    stdout.close()
    stderr.close()
    return subprocess.CompletedProcess(argv, code, stdout.snapshot(), stderr.snapshot())


class NativeXrayProcess:
    def __init__(self, binary: Path, mihomo_binary: Path, root: Path, config_path: Path,
                 mihomo_config_path: Path, socket_path: Path) -> None:
        self.binary = binary
        self.mihomo_binary = mihomo_binary
        self.root = root
        self.config_path = config_path
        self.mihomo_config_path = mihomo_config_path
        self.socket_path = socket_path
        self.process: subprocess.Popen[bytes] | None = None
        self.started_at: str | None = None
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause_reload = False
        self._hold_reload_after = 1
        self._release_reload = threading.Event()
        self._release_reload.set()
        self.reload_entered = threading.Event()
        self._fail_next_reload = False
        self._reload_calls = 0
        self._checkpoint_phase: str | None = None
        self.checkpoint_entered = threading.Event()
        self._release_checkpoint = threading.Event()
        self._release_checkpoint.set()
        self._hold_action: str | None = None
        self._fail_action: str | None = None
        self.action_entered = threading.Event()
        self._release_action = threading.Event()
        self._release_action.set()
        self._last_mihomo_probe_result: dict[str, Any] | None = None
        self._last_selection_commit: dict[str, Any] | None = None
        self._guard = threading.Lock()
        self._xray_process_lock = threading.Lock()
        self._mihomo_process_lock = threading.Lock()
        self._rpc_slots = threading.BoundedSemaphore(8)
        self._rpc_threads: set[threading.Thread] = set()
        self._native_config_path: Path | None = None
        self._xray_provider_upstream_summary = {
            "inbound_present": False, "client_count": 0, "fixture_identity_present": False,
        }
        self._native_argv: list[str] | None = None
        self.mihomo_process: subprocess.Popen[bytes] | None = None
        self.mihomo_started_at: str | None = None
        self._mihomo_native_config_path: Path | None = None
        self._mihomo_requested_config_path: Path | None = None
        self._corrupt_next_candidate = False
        self._process_pipes: dict[str, tuple[_BoundedPipe, _BoundedPipe]] = {}
        self._process_history: list[dict[str, Any]] = []
        self._command_history: list[dict[str, Any]] = []

    def start(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._launch()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self._server.listen(4)
        self._server.settimeout(0.25)
        self._thread = threading.Thread(target=self._serve, name="acceptance-xray-rpc", daemon=True)
        self._thread.start()
        self._wait_api()
        self._launch_mihomo(self.mihomo_config_path)
        self._wait_mihomo_controller()

    def _launch(self) -> None:
        with self._xray_process_lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self._seal_process_capture("xray")
            native_dir = self.root / "native-configs"
            native_dir.mkdir(mode=0o700, exist_ok=True)
            snapshot = native_dir / f"xray-{time.monotonic_ns()}.json"
            config_bytes = self.config_path.read_bytes()
            snapshot.write_bytes(config_bytes)
            self._xray_provider_upstream_summary = self._provider_upstream_summary(config_bytes)
            snapshot.chmod(0o600)
            self._native_config_path = snapshot
            self._native_argv = [str(self.binary), "run", "-config", str(snapshot)]
            self.process = subprocess.Popen(
                self._native_argv, cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
                     "LANG": "C.UTF-8", "TZ": "UTC"},
            )
            self._capture_process("xray", self.process)
            self.started_at = self._process_started_at(self.process.pid)

    @staticmethod
    def _provider_upstream_summary(config_bytes: bytes) -> dict[str, Any]:
        """Summarize the controlled fixture inbound without exposing its identity."""
        summary = {"inbound_present": False, "client_count": 0, "fixture_identity_present": False}
        try:
            config = json.loads(config_bytes)
        except (TypeError, ValueError, UnicodeDecodeError):
            return summary
        inbounds = config.get("inbounds") if isinstance(config, dict) else None
        if not isinstance(inbounds, list):
            return summary
        for inbound in inbounds:
            if not isinstance(inbound, dict) or inbound.get("tag") != "acceptance-provider-upstream":
                continue
            summary["inbound_present"] = True
            settings = inbound.get("settings")
            clients = settings.get("clients") if isinstance(settings, dict) else None
            clients = clients if isinstance(clients, list) else []
            summary["client_count"] = min(len(clients), 100_000)
            summary["fixture_identity_present"] = any(
                isinstance(client, dict)
                and client.get("id") == "88c7ce2a-465e-4e72-9c56-2a9e2fc84a51"
                and client.get("email") == "acceptance-provider-upstream"
                for client in clients
            )
            break
        return summary

    @staticmethod
    def _process_started_at(pid: int) -> str:
        proc_stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = proc_stat[proc_stat.rfind(")") + 1:].split()
        start_ticks = int(tail[19])
        boot_line = next(line for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        epoch = int(boot_line.split()[1]) + start_ticks / os.sysconf("SC_CLK_TCK")
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def _wait_api(self) -> None:
        deadline = time.monotonic() + 10
        last = ""
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                last = "Xray child exited"
                break
            result = self._xray_cli(["api", "inbounduser", "--server=127.0.0.1:10085", "-timeout=1", "-tag=vless-ws"], timeout=1.5)
            if result.returncode == 0:
                return
            last = result.stderr.decode("utf-8", "replace")[-512:]
            self._stop.wait(0.1)
        raise RuntimeError(f"pinned Xray HandlerService did not become ready: {last}")

    def _xray_cli(self, args: list[str], *, timeout: float = 8) -> subprocess.CompletedProcess[bytes]:
        return self._native_cli("xray", self.binary, args, timeout=timeout,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
                 "LANG": "C.UTF-8", "TZ": "UTC"})

    def _test_xray_candidate(self, candidate: Path) -> dict[str, Any]:
        """Validate the exact candidate through Xray's production-equivalent JSON path.

        RealXrayAdapter mounts each candidate read-only at a path ending in
        ``.json``. Xray infers its config format from that suffix, so preserve
        the candidate and validate a private byte-identical copy with the same
        suffix in this process-owned root.
        """
        root = self.root.resolve(strict=True)
        candidate = candidate.resolve(strict=True)
        info = candidate.stat()
        if (not candidate.is_relative_to(root) or not candidate.is_file()
                or info.st_size > MAX_REQUEST * 64):
            raise ValueError("candidate path is outside acceptance state")
        candidate_limit = MAX_REQUEST * 64

        def read_candidate() -> bytes:
            with candidate.open("rb") as stream:
                content = stream.read(candidate_limit + 1)
            if len(content) > candidate_limit:
                raise ValueError("candidate exceeds native validation size limit")
            return content

        original = read_candidate()
        original_digest = hashlib.sha256(original).hexdigest()
        native_dir = self.root / "native-configs"
        native_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="xray-candidate-", suffix=".json", dir=native_dir)
        validation_path = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            copied_digest = hashlib.sha256(validation_path.read_bytes()).hexdigest()
            retained_digest = hashlib.sha256(read_candidate()).hexdigest()
            if copied_digest != original_digest or retained_digest != original_digest:
                raise ValueError("candidate changed while preparing native Xray validation")
            proc = self._xray_cli(["-test", "-config", str(validation_path)], timeout=8)
            return self._completed(proc)
        finally:
            validation_path.unlink(missing_ok=True)

    def _native_cli(self, name: str, binary: Path, args: list[str], *, timeout: float,
                    env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        argv = [str(binary), *args]
        proc = _bounded_child(argv, cwd=self.root,
            env=env, timeout=timeout)
        safe_args = [item.split("=", 1)[0] for item in args if item.startswith("-")][:16]
        event = {"service": name, "executable": binary.name, "argument_flags": safe_args,
                 "exit_code": proc.returncode,
                 "stdout": _redact_diagnostic(proc.stdout, limit=4096),
                 "stderr": _redact_diagnostic(proc.stderr, limit=4096)}
        with self._guard:
            self._command_history.append(event)
            if len(self._command_history) > DIAGNOSTIC_COMMAND_LIMIT:
                del self._command_history[:-DIAGNOSTIC_COMMAND_LIMIT]
        return proc

    def _capture_process(self, name: str, child: subprocess.Popen[bytes],
                         stdout=None, stderr=None) -> None:
        streams: tuple[_BoundedPipe, _BoundedPipe]
        if stdout is not None and stderr is not None:
            streams = (stdout, stderr)
        else:
            assert child.stdout is not None and child.stderr is not None
            streams = (_BoundedPipe(child.stdout), _BoundedPipe(child.stderr))
        self._process_pipes[name] = streams

    def _seal_process_capture(self, name: str) -> None:
        streams = self._process_pipes.pop(name, None)
        if streams is None:
            return
        process = self.process if name == "xray" else self.mihomo_process
        for stream in streams:
            stream.close()
        with self._guard:
            self._process_history.append({
                "service": name,
                "pid": process.pid if process is not None else None,
                "exit_code": process.poll() if process is not None else None,
                "stdout": _redact_diagnostic(streams[0].snapshot()),
                "stderr": _redact_diagnostic(streams[1].snapshot()),
                **({"provider_upstream": dict(self._xray_provider_upstream_summary)} if name == "xray" else {}),
            })
            if len(self._process_history) > 12:
                del self._process_history[:-12]

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Return a bounded, redacted snapshot without copying app state/config."""
        current: list[dict[str, Any]] = []
        for name, streams in list(self._process_pipes.items()):
            process = self.process if name == "xray" else self.mihomo_process
            current.append({"service": name, "pid": process.pid if process is not None else None,
                            "exit_code": process.poll() if process is not None else None,
                            "stdout": _redact_diagnostic(streams[0].snapshot()),
                            "stderr": _redact_diagnostic(streams[1].snapshot()),
                            **({"provider_upstream": dict(self._xray_provider_upstream_summary)} if name == "xray" else {})})
        with self._guard:
            value = {"schema": "fwrouter-native-process-diagnostics/v1",
                     "processes": [*self._process_history, *current],
                     "commands": list(self._command_history)}
        # Enforce a hard serialized cap by dropping oldest records, never by
        # slicing JSON into an invalid or partially redacted document.
        while True:
            payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(payload) <= DIAGNOSTIC_TOTAL_LIMIT:
                return value
            if value["commands"]:
                value["commands"].pop(0)
            elif value["processes"]:
                value["processes"].pop(0)
            else:
                return {"schema": "fwrouter-native-process-diagnostics/v1", "processes": [],
                        "commands": [], "truncated": True}

    def write_diagnostics(self, destination: Path) -> Path:
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = destination / "native-process-diagnostics.json"
        payload = json.dumps(self.diagnostic_snapshot(), sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > DIAGNOSTIC_TOTAL_LIMIT:
            raise ValueError("native process diagnostic receipt exceeds its total size bound")
        path.write_bytes(payload)
        path.chmod(0o600)
        return path

    def _launch_mihomo(self, config_path: Path) -> None:
        with self._mihomo_process_lock:
            if self.mihomo_process is not None and self.mihomo_process.poll() is None:
                self.mihomo_process.terminate()
                try:
                    self.mihomo_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.mihomo_process.kill()
                    self.mihomo_process.wait(timeout=2)
            self._seal_process_capture("mihomo")
            resolved = config_path.resolve(strict=True)
            if not resolved.is_relative_to(self.root.resolve()):
                raise ValueError("Mihomo config path is outside acceptance state")
            native_dir = self.root / "native-mihomo-configs"
            native_dir.mkdir(mode=0o700, exist_ok=True)
            snapshot = native_dir / f"mihomo-{time.monotonic_ns()}.yaml"
            snapshot.write_bytes(resolved.read_bytes())
            snapshot.chmod(0o600)
            self._mihomo_requested_config_path = resolved
            self._mihomo_native_config_path = snapshot
            self.mihomo_process = subprocess.Popen(
                [str(self.mihomo_binary), "-f", str(snapshot)], cwd=self.root,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True, start_new_session=True,
                env={"PATH": "/opt/fwrouter-test/bin:/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
                     "LANG": "C.UTF-8", "TZ": "UTC"},
            )
            self._capture_process("mihomo", self.mihomo_process)
            self.mihomo_started_at = self._process_started_at(self.mihomo_process.pid)

    def _wait_mihomo_controller(self) -> None:
        deadline = time.monotonic() + 12
        last = "controller unavailable"
        while time.monotonic() < deadline:
            if self.mihomo_process is None or self.mihomo_process.poll() is not None:
                raise RuntimeError("pinned Mihomo child exited before controller readiness")
            try:
                with urllib.request.urlopen("http://127.0.0.1:5200/version", timeout=0.5) as response:
                    payload = json.loads(response.read(64 * 1024))
                if response.status == 200 and isinstance(payload, dict) and payload.get("version"):
                    return
                last = "Mihomo controller returned an invalid version response"
            except Exception as exc:
                last = type(exc).__name__
                self._stop.wait(0.1)
        raise RuntimeError(f"pinned Mihomo controller did not become ready: {last}")

    def hold_next_reload(self) -> None:
        self.hold_reload_after(1)

    def hold_reload_after(self, successful_calls: int) -> None:
        """Pause the selected reload invocation; calls before it use the real child."""
        if type(successful_calls) is not int or successful_calls < 1 or successful_calls > 16:
            raise ValueError("reload barrier count must be between 1 and 16")
        with self._guard:
            self._pause_reload = True
            self._hold_reload_after = successful_calls
            self._release_reload.clear()
            self.reload_entered.clear()

    def corrupt_next_candidate(self) -> None:
        """Inject malformed bytes only into the next isolated candidate path."""
        with self._guard:
            self._corrupt_next_candidate = True

    def release_reload(self) -> None:
        self._release_reload.set()

    def fail_next_reload(self) -> None:
        """Fail one real adapter reload call before changing the owned process."""
        with self._guard:
            self._fail_next_reload = True

    @property
    def reload_calls(self) -> int:
        with self._guard:
            return self._reload_calls

    @property
    def last_mihomo_probe_result(self) -> dict[str, Any] | None:
        """Return the last real Mihomo HTTP probe result observed by the worker."""
        with self._guard:
            return dict(self._last_mihomo_probe_result) if self._last_mihomo_probe_result is not None else None

    @property
    def provider_upstream_summary(self) -> dict[str, Any]:
        with self._guard:
            return dict(self._xray_provider_upstream_summary)

    @property
    def last_selection_commit(self) -> dict[str, Any] | None:
        """Return the actual Core commit inputs observed at the CAS boundary."""
        with self._guard:
            return dict(self._last_selection_commit) if self._last_selection_commit is not None else None

    def hold_checkpoint_phase(self, phase: str) -> None:
        if phase not in {"prepared", "transition_applied", "xray_applied", "runtime_applied",
                         "inventory_synced", "bindings_written", "projections_cleaned", "selection_verified"}:
            raise ValueError("unsupported Xray generation checkpoint phase")
        with self._guard:
            self._checkpoint_phase = phase
            self.checkpoint_entered.clear()
            self._release_checkpoint.clear()

    def release_checkpoint(self) -> None:
        self._release_checkpoint.set()

    def hold_action(self, action: str) -> None:
        if action not in {"test_config", "api_inbound_users", "runtime_container_id", "runtime_inspect", "runtime_config_archive", "mihomo_probe_result", "selection_commit_barrier"}:
            raise ValueError("unsupported Xray RPC action barrier")
        with self._guard:
            self._hold_action = action
            self.action_entered.clear()
            self._release_action.clear()

    def release_action(self) -> None:
        self._release_action.set()

    def fail_next_action(self, action: str) -> None:
        """Return one transport failure at a real readback/process boundary."""
        if action not in {"api_inbound_users", "runtime_container_id", "runtime_inspect", "runtime_config_archive"}:
            raise ValueError("unsupported Xray transport failure boundary")
        with self._guard:
            self._fail_action = action

    def restart_xray_child(self) -> None:
        """Restart the owned native process from the actual active config."""
        self._launch()
        self._wait_api()

    def restart_mihomo_child(self) -> None:
        """Restart owned Mihomo directly while its parent RPC thread is barred."""
        if self._mihomo_requested_config_path is None:
            raise RuntimeError("owned Mihomo source config is unavailable")
        self._launch_mihomo(self._mihomo_requested_config_path)
        self._wait_mihomo_controller()

    def rpc(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Exercise the same framed, bounded UDS interface used by the API worker."""
        request = json.dumps({"action": action, "payload": payload}, separators=(",", ":")).encode() + b"\n"
        if len(request) > MAX_REQUEST:
            raise ValueError("native runner request exceeds bounds")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(25)
            client.connect(str(self.socket_path))
            client.sendall(request)
            response = bytearray()
            while len(response) <= MAX_RESPONSE:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if b"\n" in response:
                    break
        if len(response) > MAX_RESPONSE:
            raise ValueError("native runner response exceeds bounds")
        value = json.loads(bytes(response).split(b"\n", 1)[0])
        details = value.get("details") if isinstance(value.get("details"), dict) else {}
        if "archive_bytes_b64" in details:
            details["archive_bytes"] = base64.b64decode(details.pop("archive_bytes_b64"), validate=True)
        if "config_bytes_b64" in details:
            details["config_bytes"] = base64.b64decode(details.pop("config_bytes_b64"), validate=True)
        value["details"] = details
        return value

    def stop(self) -> None:
        self._stop.set()
        self._release_reload.set()
        self._release_checkpoint.set()
        self._release_action.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self._seal_process_capture("xray")
        if self.mihomo_process is not None and self.mihomo_process.poll() is None:
            self.mihomo_process.terminate()
            try:
                self.mihomo_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.mihomo_process.kill()
                self.mihomo_process.wait(timeout=2)
        self._seal_process_capture("mihomo")
        with self._guard:
            rpc_threads = list(self._rpc_threads)
        rpc_deadline = time.monotonic() + 5
        for thread in rpc_threads:
            thread.join(timeout=max(0.0, rpc_deadline - time.monotonic()))
        self.socket_path.unlink(missing_ok=True)

    @property
    def live_rpc_threads(self) -> int:
        with self._guard:
            return sum(1 for thread in self._rpc_threads if thread.is_alive())

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._rpc_slots.acquire(blocking=False):
                try:
                    connection.sendall(b'{"ok":false,"error":"RPC concurrency limit reached"}\n')
                except OSError:
                    pass
                connection.close()
                continue
            thread = threading.Thread(target=self._handle_rpc_connection, args=(connection,), daemon=True)
            with self._guard:
                self._rpc_threads.add(thread)
            thread.start()

    def _handle_rpc_connection(self, connection: socket.socket) -> None:
        current = threading.current_thread()
        try:
            with connection:
                connection.settimeout(25)
                try:
                    raw = bytearray()
                    while len(raw) <= MAX_REQUEST:
                        part = connection.recv(4096)
                        if not part:
                            break
                        raw.extend(part)
                        if b"\n" in raw:
                            break
                    if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                        raise ValueError("RPC request exceeds bounds")
                    request = json.loads(raw[:-1])
                    response = self._dispatch(request)
                    encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
                    if len(encoded) > MAX_RESPONSE:
                        raise ValueError("RPC response exceeds bounds")
                    connection.sendall(encoded)
                except Exception as exc:
                    encoded = json.dumps({"ok": False, "error": type(exc).__name__}).encode() + b"\n"
                    try:
                        connection.sendall(encoded[:1024])
                    except OSError:
                        pass
        finally:
            self._rpc_slots.release()
            with self._guard:
                self._rpc_threads.discard(current)

    def _dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) != {"action", "payload"}:
            raise ValueError("invalid RPC envelope")
        action, payload = request["action"], request["payload"]
        if action not in RPC_ACTIONS or not isinstance(payload, dict):
            raise ValueError("unsupported RPC action")
        if action == "selection_commit_barrier":
            if (set(payload) != {"expected_revision", "expected_active_server_id", "expected_provenance_decision_id", "server_id", "operation_id"}
                    or type(payload.get("expected_revision")) is not int
                    or not isinstance(payload.get("server_id"), str)
                    or not isinstance(payload.get("operation_id"), str)):
                raise ValueError("invalid Core selection commit barrier payload")
            with self._guard:
                self._last_selection_commit = dict(payload)
        with self._guard:
            held_action = self._hold_action == action
            if held_action:
                self._hold_action = None
            failed_action = self._fail_action == action
            if failed_action:
                self._fail_action = None
        if held_action:
            self.action_entered.set()
            if not self._release_action.wait(20):
                return {"ok": False, "message": "native RPC action barrier timed out", "error_code": "HARNESS_ACTION_TIMEOUT", "details": {"action": action}}
        if failed_action:
            return {"ok": False, "message": "controlled native readback transport failure",
                    "error_code": "HARNESS_XRAY_READBACK_UNAVAILABLE", "details": {"fault_boundary": action}}
        if action == "test_config":
            path = Path(str(payload.get("path", ""))).resolve(strict=True)
            if not path.is_relative_to(self.root.resolve()) or path.stat().st_size > MAX_REQUEST * 64:
                raise ValueError("candidate path is outside acceptance state")
            with self._guard:
                corrupt = self._corrupt_next_candidate
                self._corrupt_next_candidate = False
            if corrupt:
                if path.name != "config.json.candidate" or path == self.config_path.resolve():
                    raise ValueError("fault injection is restricted to the isolated Xray candidate artifact")
                path.write_bytes(b"{ controlled invalid candidate")
            return self._test_xray_candidate(path)
        if action == "reload":
            with self._guard:
                self._reload_calls += 1
                if self._pause_reload:
                    self._hold_reload_after -= 1
                    pause = self._hold_reload_after == 0
                    if pause:
                        self._pause_reload = False
                else:
                    pause = False
                fail = self._fail_next_reload
                self._fail_next_reload = False
            if pause:
                self.reload_entered.set()
                if not self._release_reload.wait(15):
                    return {"ok": False, "message": "reload pause timed out", "error_code": "HARNESS_PAUSE_TIMEOUT", "details": {}}
            if fail:
                return {"ok": False, "message": "controlled Xray reload transport failure", "error_code": "HARNESS_XRAY_RELOAD_FAILURE", "details": {"fault_boundary": "reload"}}
            self._launch()
            try:
                self._wait_api()
                return {"ok": True, "message": "pinned Xray process restarted", "details": {}}
            except Exception as exc:
                return {"ok": False, "message": "pinned Xray process did not become ready", "error_code": "XRAY_RELOAD_FAILED", "details": {"reason": str(exc)[:512]}}
        if action == "compose_ps":
            running = self.process is not None and self.process.poll() is None
            return {"ok": True, "message": "process status", "details": {"stdout": "xray running" if running else "xray stopped"}}
        if action == "api_inbound_users":
            if payload != {"tag": "vless-ws"}:
                raise ValueError("unsupported inbound query")
            return self._completed(self._xray_cli(["api", "inbounduser", "--server=127.0.0.1:10085", "-timeout=3", "-tag=vless-ws"], timeout=8))
        if action == "mihomo_probe_result":
            if (set(payload) != {"logical_runtime_target", "probe_ok"}
                    or not isinstance(payload.get("logical_runtime_target"), str)
                    or not isinstance(payload.get("probe_ok"), bool)):
                raise ValueError("invalid native Mihomo probe barrier payload")
            with self._guard:
                self._last_mihomo_probe_result = dict(payload)
            return {"ok": True, "message": "actual Mihomo probe result observed", "details": {"barrier": True}}
        if action == "selection_commit_barrier":
            return {"ok": True, "message": "actual Core CAS inputs observed", "details": {"barrier": True}}
        if action == "mihomo_config_snapshot":
            if payload != {} or self._mihomo_native_config_path is None:
                raise ValueError("invalid native Mihomo config snapshot request")
            if self.mihomo_process is None or self.mihomo_process.poll() is not None:
                return {"ok": False, "message": "Mihomo child is stopped", "error_code": "MIHOMO_RUNTIME_NOT_RUNNING", "details": {}}
            config_bytes = self._mihomo_native_config_path.read_bytes()
            return {"ok": True, "message": "owned Mihomo launch snapshot", "details": {
                "native_config_path": str(self._mihomo_native_config_path),
                "config_bytes_b64": base64.b64encode(config_bytes).decode("ascii"),
            }}
        if action == "runtime_container_id":
            if self.process is None or self.process.poll() is not None:
                return {"ok": False, "message": "Xray process is stopped", "error_code": "XRAY_RUNTIME_NOT_RUNNING", "details": {}}
            return {"ok": True, "message": "owned process identity", "details": {"stdout": f"{self.process.pid:x}"}}
        if action == "runtime_inspect":
            pid_hex = str(payload.get("container_id") or "")
            if self.process is None or pid_hex != f"{self.process.pid:x}" or self.process.poll() is not None:
                return {"ok": False, "message": "process identity mismatch", "error_code": "XRAY_RUNTIME_ID_INVALID", "details": {}}
            try:
                started_at = self._process_started_at(self.process.pid)
            except (OSError, ValueError, IndexError, StopIteration):
                return {"ok": False, "message": "could not revalidate process start time", "error_code": "XRAY_RUNTIME_NOT_RUNNING", "details": {}}
            if started_at != self.started_at:
                return {"ok": False, "message": "process start identity changed", "error_code": "XRAY_RUNTIME_ID_CHANGED_DURING_READ", "details": {}}
            return {"ok": True, "message": "owned process incarnation", "details": {"stdout": f"{pid_hex}|true|{started_at}"}}
        if action == "runtime_config_archive":
            pid_hex = str(payload.get("container_id") or "")
            if self.process is None or pid_hex != f"{self.process.pid:x}" or self.process.poll() is not None or self._native_config_path is None:
                return {"ok": False, "message": "process config unavailable", "error_code": "XRAY_RUNTIME_CONFIG_READ_FAILED", "details": {}}
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as bundle:
                with tarfile.open(fileobj=bundle, mode="w") as archive:
                    data = self._native_config_path.read_bytes()
                    info = tarfile.TarInfo("config.json")
                    info.size = len(data)
                    info.mtime = 0
                    archive.addfile(info, __import__("io").BytesIO(data))
                bundle.seek(0)
                data = bundle.read(MAX_RESPONSE)
            if self.process.poll() is not None or pid_hex != f"{self.process.pid:x}":
                return {"ok": False, "message": "process changed during config archive read", "error_code": "XRAY_RUNTIME_ID_CHANGED_DURING_READ", "details": {}}
            return {"ok": True, "message": "native process launch config archive", "details": {"archive_bytes_b64": base64.b64encode(data).decode("ascii"), "native_config_path": str(self._native_config_path), "native_argv": self._native_argv}}
        if action == "mihomo_test_config":
            path = Path(str(payload.get("path", ""))).resolve(strict=True)
            if not path.is_relative_to(self.root.resolve()) or path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Mihomo candidate path is outside acceptance state")
            proc = self._native_cli("mihomo", self.mihomo_binary, ["-t", "-f", str(path)], timeout=15,
                env={"PATH": "/opt/fwrouter-test/bin:/usr/bin:/bin", "HOME": str(self.root),
                     "TMPDIR": str(self.root), "LANG": "C.UTF-8", "TZ": "UTC"})
            return self._completed(proc)
        if action == "mihomo_restart":
            path = Path(str(payload.get("config_path", "")))
            if path.resolve(strict=True) != self.mihomo_config_path.resolve(strict=True):
                raise ValueError("Mihomo restart path is not the owned active config")
            try:
                self._launch_mihomo(path)
                self._wait_mihomo_controller()
                return {"ok": True, "message": "pinned Mihomo process restarted and controller verified", "details": {}}
            except Exception as exc:
                return {"ok": False, "message": "pinned Mihomo did not become ready", "error_code": "MIHOMO_RESTART_FAILED", "details": {"reason": str(exc)[:512]}}
        if action == "mihomo_status":
            running = self.mihomo_process is not None and self.mihomo_process.poll() is None
            return {"ok": running, "message": "owned Mihomo process status", "error_code": None if running else "MIHOMO_RUNTIME_NOT_RUNNING", "details": {"stdout": "mihomo running" if running else "mihomo stopped"}}
        if action == "mihomo_incarnation":
            if self.mihomo_process is None or self.mihomo_process.poll() is not None:
                return {"ok": False, "message": "Mihomo process is stopped", "error_code": "MIHOMO_RUNTIME_NOT_RUNNING", "details": {}}
            pid = self.mihomo_process.pid
            started = self._process_started_at(pid)
            if started != self.mihomo_started_at:
                return {"ok": False, "message": "Mihomo process identity changed", "error_code": "MIHOMO_RUNTIME_ID_CHANGED", "details": {}}
            return {"ok": True, "message": "owned Mihomo process incarnation", "details": {"stdout": f"{pid:x}|{started}"}}
        if action == "generation_checkpoint":
            phase = str(payload.get("phase") or "")
            with self._guard:
                selected = self._checkpoint_phase == phase
                if selected:
                    self._checkpoint_phase = None
            if not selected:
                return {"ok": True, "message": "checkpoint observed", "details": {"phase": phase, "held": False}}
            self.checkpoint_entered.set()
            if not self._release_checkpoint.wait(20):
                return {"ok": False, "message": "checkpoint barrier timed out", "error_code": "HARNESS_CHECKPOINT_TIMEOUT", "details": {"phase": phase}}
            return {"ok": True, "message": "checkpoint barrier released", "details": {"phase": phase, "held": True}}
        raise ValueError("unreachable RPC action")

    @staticmethod
    def _completed(proc: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
        return {"ok": proc.returncode == 0, "message": "native command completed", "error_code": None if proc.returncode == 0 else f"XRAY_NATIVE_EXIT_{proc.returncode}",
                "details": {"stdout": proc.stdout[:DIAGNOSTIC_STREAM_LIMIT].decode("utf-8", "replace"),
                            "stderr": proc.stderr[:4096].decode("utf-8", "replace")}}


class XrayRPCClient:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def __call__(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in RPC_ACTIONS:
            raise ValueError("unsupported Xray RPC action")
        if action == "runtime_config_archive":
            raise ValueError("archive must be decoded by the worker adapter")
        request = json.dumps({"action": action, "payload": payload}, separators=(",", ":")).encode() + b"\n"
        if len(request) > MAX_REQUEST:
            raise ValueError("Xray RPC request exceeds bounds")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(9)
            client.connect(str(self.socket_path))
            client.sendall(request)
            response = bytearray()
            while len(response) <= MAX_RESPONSE:
                part = client.recv(65536)
                if not part or b"\n" in part:
                    response.extend(part)
                    break
                response.extend(part)
            if len(response) > MAX_RESPONSE:
                raise ValueError("Xray RPC response exceeds bounds")
        return json.loads(response.split(b"\n", 1)[0])

    def archive(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = json.dumps({"action": action, "payload": payload}, separators=(",", ":")).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(9)
            client.connect(str(self.socket_path))
            client.sendall(request)
            response = client.recv(MAX_RESPONSE)
        item = json.loads(response.split(b"\n", 1)[0])
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        if details.get("archive_bytes_b64"):
            details["archive_bytes"] = base64.b64decode(details.pop("archive_bytes_b64"), validate=True)
        item["details"] = details
        return item
