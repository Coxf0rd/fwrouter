"""Loopback-only external provider HTTP boundary for joined acceptance.

The application still uses its real StealthSurf HTTP client, parsing, Core,
jobs and persistence. This server supplies deterministic provider responses.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .http_support import http_json


class ProviderHttpTestBridge:
    def __init__(self) -> None:
        self.mode = "normal"
        self.calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []
        self._lock = threading.Lock()
        self.request_entered = threading.Event()
        self._release_response = threading.Event()
        self.probe_available = True
        self.probe_entered = threading.Event()
        self._release_probe = threading.Event()
        self._release_probe.set()
        self._hold_probe_count = 0
        self.probe_responses: list[int] = []
        self.probe_requests = 0
        self.current_config: dict[str, object] = {
            "id": 42, "name": "acceptance-profile", "server_id": 901,
            "location_id": 6, "protocol": "vless",
            "connection_url": "vless://88c7ce2a-465e-4e72-9c56-2a9e2fc84a51@127.0.0.1:5301?type=tcp&encryption=none#acceptance",
        }
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("provider bridge is not started")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> "ProviderHttpTestBridge":
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(10)

            def _respond(self, status: int, payload: object, headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # Expected when the real client deadline expires first.
                    return

            def _handle(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    self._respond(400, {"status": False, "data": None})
                    return
                if length < 0 or length > 64 * 1024:
                    self._respond(413, {"status": False, "data": None})
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw) if raw else None
                except (ValueError, UnicodeDecodeError):
                    payload = None
                parsed = urlparse(self.path)
                if parsed.path == "/generate_204" and self.command in {"GET", "HEAD"}:
                    with bridge._lock:
                        bridge.probe_requests += 1
                        should_hold = bridge._hold_probe_count > 0
                        if should_hold:
                            bridge._hold_probe_count -= 1
                    if should_hold:
                        bridge.probe_entered.set()
                        released = bridge._release_probe.wait(timeout=20)
                        if not released:
                            with bridge._lock:
                                bridge.probe_responses.append(504)
                            self._respond(504, {"status": False, "data": None})
                            self.close_connection = True
                            return
                    with bridge._lock:
                        available = bridge.probe_available
                    if available:
                        with bridge._lock:
                            bridge.probe_responses.append(204)
                        self.send_response(204)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        with bridge._lock:
                            bridge.probe_responses.append(503)
                        self.send_error(503)
                    return
                with bridge._lock:
                    mode = bridge.mode
                    bridge.calls.append((self.command, parsed.path, dict(parsed.query and [
                        part.split("=", 1) if "=" in part else (part, "")
                        for part in parsed.query.split("&")
                    ] or []), payload if isinstance(payload, dict) else None))
                effective_mode = mode
                if mode.startswith("patch_") and self.command != "PATCH":
                    effective_mode = "normal"
                if mode == "slow_success":
                    bridge.request_entered.set()
                    if not bridge._release_response.wait(timeout=20):
                        self._respond(504, {"status": False, "data": None})
                        self.close_connection = True
                        return
                    effective_mode = "normal"
                if effective_mode in {"timeout", "patch_timeout"}:
                    bridge.request_entered.set()
                    if not bridge._release_response.wait(timeout=20):
                        self._respond(504, {"status": False, "data": None})
                        self.close_connection = True
                    return
                if effective_mode in {"429", "patch_429"}:
                    self._respond(429, {"status": False, "data": None}, {"Retry-After": "30"})
                    return
                if effective_mode in {"503", "patch_503"}:
                    self._respond(503, {"status": False, "data": None})
                    return
                if effective_mode in {"malformed", "patch_malformed"}:
                    body = b"{not-json"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/configs" and self.command == "GET":
                    with bridge._lock:
                        rows = [dict(bridge.current_config)]
                    self._respond(200, {"status": True, "statusCode": 200, "data": rows})
                elif parsed.path == "/locations" and self.command == "GET":
                    self._respond(200, {"status": True, "statusCode": 200,
                                        "data": [{"id": 6, "name": "acceptance-location"}]})
                elif parsed.path == "/configs/available-servers" and self.command == "GET":
                    status = "down" if mode == "down" else "unknown" if mode == "unknown" else "up"
                    self._respond(200, {"status": True, "statusCode": 200,
                                        "data": [{"id": 901, "ip": "192.0.2.91", "available_slots": 4,
                                                  "status": status},
                                                 {"id": 902, "ip": "192.0.2.92", "available_slots": 3,
                                                  "status": status}]})
                elif parsed.path.endswith("/serverStats") and self.command == "GET":
                    status = "down" if mode == "down" else "mystery" if mode == "unknown" else "up"
                    self._respond(200, {"status": True, "statusCode": 200, "data": {"status": status}})
                elif parsed.path.endswith("/settings") and self.command == "PATCH":
                    # Successful mutation must be visible through subsequent
                    # read-only confirmation. Fault modes return above and do
                    # not accidentally acknowledge or persist a successful PATCH.
                    with bridge._lock:
                        row = dict(bridge.current_config)
                        if isinstance(payload, dict):
                            row.update(payload)
                        bridge.current_config = dict(row)
                    self._respond(200, {"status": True, "statusCode": 200, "data": row})
                else:
                    self._respond(404, {"status": False, "statusCode": 404, "data": None})

            do_GET = _handle
            do_HEAD = _handle
            do_PATCH = _handle
            do_POST = _handle

            def log_message(self, fmt: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = False
        self._server.block_on_close = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def set_mode(self, mode: str) -> None:
        if mode not in {"normal", "timeout", "429", "503", "malformed", "down", "unknown",
                        "patch_timeout", "patch_429", "patch_503", "patch_malformed", "slow_success"}:
            raise ValueError("unsupported provider bridge mode")
        with self._lock:
            self.mode = mode
            self.request_entered.clear()
            self._release_response.clear()

    def release_response(self) -> None:
        self._release_response.set()

    def set_probe_available(self, available: bool) -> None:
        with self._lock:
            self.probe_available = bool(available)

    def hold_probe(self) -> None:
        self.probe_entered.clear()
        self._release_probe.clear()
        with self._lock:
            self._hold_probe_count = 1

    def release_probe(self) -> None:
        self._release_probe.set()
        with self._lock:
            self._hold_probe_count = 0

    def snapshot_probe_responses(self) -> list[int]:
        with self._lock:
            return list(self.probe_responses)

    def snapshot_probe_summary(self) -> dict[str, object]:
        with self._lock:
            responses = list(self.probe_responses)
            return {
                "request_count": self.probe_requests,
                "response_count": len(responses),
                "response_status_counts": {
                    str(status): responses.count(status) for status in sorted(set(responses))
                },
            }

    def snapshot_calls(self) -> list[tuple[str, str, dict[str, str], dict[str, object] | None]]:
        with self._lock:
            return list(self.calls)

    def close(self) -> None:
        self._release_response.set()
        self._release_probe.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def await_core_job(api: str, response: dict, *, timeout_seconds: int = 75) -> dict:
    """Wait on the qualified worker's job condition instead of polling/sleeping."""
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    job_id = str(job.get("job_id") or "")
    assert job_id, f"API did not return the accepted job: {response!r}"
    code, result = http_json(f"{api}/__acceptance/jobs/{job_id}/wait", method="POST",
                             payload={"timeout_seconds": timeout_seconds}, timeout=timeout_seconds + 5)
    assert code == 200 and result.get("ok") is True, result
    terminal = result.get("job")
    assert isinstance(terminal, dict), result
    return terminal
