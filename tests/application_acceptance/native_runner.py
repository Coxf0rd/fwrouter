"""Parent-owned Xray process and bounded AF_UNIX RPC transport for app workers."""
from __future__ import annotations

import base64
import json
import os
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
               "runtime_inspect", "runtime_config_archive", "mihomo_test_config", "mihomo_restart",
               "mihomo_status", "mihomo_incarnation"}


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
        self._release_reload = threading.Event()
        self._release_reload.set()
        self.reload_entered = threading.Event()
        self._guard = threading.Lock()
        self._native_config_path: Path | None = None
        self._native_argv: list[str] | None = None
        self.mihomo_process: subprocess.Popen[bytes] | None = None
        self.mihomo_started_at: str | None = None
        self._mihomo_native_config_path: Path | None = None
        self._corrupt_next_candidate = False

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
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        native_dir = self.root / "native-configs"
        native_dir.mkdir(mode=0o700, exist_ok=True)
        snapshot = native_dir / f"xray-{time.monotonic_ns()}.json"
        snapshot.write_bytes(self.config_path.read_bytes())
        snapshot.chmod(0o600)
        self._native_config_path = snapshot
        self._native_argv = [str(self.binary), "run", "-config", str(snapshot)]
        self.process = subprocess.Popen(
            self._native_argv, cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
                 "LANG": "C.UTF-8", "TZ": "UTC"},
        )
        self.started_at = self._process_started_at(self.process.pid)

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
        return subprocess.run([str(self.binary), *args], cwd=self.root, env={"PATH": "/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root), "LANG": "C.UTF-8", "TZ": "UTC"},
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)

    def _launch_mihomo(self, config_path: Path) -> None:
        if self.mihomo_process is not None and self.mihomo_process.poll() is None:
            self.mihomo_process.terminate()
            try:
                self.mihomo_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.mihomo_process.kill()
                self.mihomo_process.wait(timeout=2)
        resolved = config_path.resolve(strict=True)
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError("Mihomo config path is outside acceptance state")
        self._mihomo_native_config_path = resolved
        self.mihomo_process = subprocess.Popen(
            [str(self.mihomo_binary), "-f", str(resolved)], cwd=self.root,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
            env={"PATH": "/opt/fwrouter-test/bin:/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
                 "LANG": "C.UTF-8", "TZ": "UTC"},
        )
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
        with self._guard:
            self._pause_reload = True
            self._release_reload.clear()
            self.reload_entered.clear()

    def corrupt_next_candidate(self) -> None:
        """Inject malformed bytes only into the next isolated candidate path."""
        with self._guard:
            self._corrupt_next_candidate = True

    def release_reload(self) -> None:
        self._release_reload.set()

    def rpc(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Exercise the same framed, bounded UDS interface used by the API worker."""
        request = json.dumps({"action": action, "payload": payload}, separators=(",", ":")).encode() + b"\n"
        if len(request) > MAX_REQUEST:
            raise ValueError("native runner request exceeds bounds")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(9)
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
        value["details"] = details
        return value

    def stop(self) -> None:
        self._stop.set()
        self._release_reload.set()
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
        if self.mihomo_process is not None and self.mihomo_process.poll() is None:
            self.mihomo_process.terminate()
            try:
                self.mihomo_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.mihomo_process.kill()
                self.mihomo_process.wait(timeout=2)
        self.socket_path.unlink(missing_ok=True)

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(8)
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

    def _dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) != {"action", "payload"}:
            raise ValueError("invalid RPC envelope")
        action, payload = request["action"], request["payload"]
        if action not in RPC_ACTIONS or not isinstance(payload, dict):
            raise ValueError("unsupported RPC action")
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
            proc = self._xray_cli(["-test", "-config", str(path)], timeout=8)
            return self._completed(proc)
        if action == "reload":
            with self._guard:
                pause = self._pause_reload
                self._pause_reload = False
            if pause:
                self.reload_entered.set()
                if not self._release_reload.wait(15):
                    return {"ok": False, "message": "reload pause timed out", "error_code": "HARNESS_PAUSE_TIMEOUT", "details": {}}
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
            proc = subprocess.run([str(self.mihomo_binary), "-t", "-f", str(path)], cwd=self.root,
                                  env={"PATH": "/opt/fwrouter-test/bin:/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root), "LANG": "C.UTF-8", "TZ": "UTC"},
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False)
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
        raise ValueError("unreachable RPC action")

    @staticmethod
    def _completed(proc: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
        return {"ok": proc.returncode == 0, "message": "native command completed", "error_code": None if proc.returncode == 0 else f"XRAY_NATIVE_EXIT_{proc.returncode}",
                "details": {"stdout": proc.stdout.decode("utf-8", "replace")[:MAX_RESPONSE], "stderr": proc.stderr.decode("utf-8", "replace")[:4096]}}


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
