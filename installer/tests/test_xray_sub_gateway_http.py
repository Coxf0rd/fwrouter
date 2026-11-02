from __future__ import annotations

import http.client
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
GATEWAY_PATH = ROOT / "host/libexec/fwrouter/fwrouter-xray-sub-gateway.py"
spec = importlib.util.spec_from_file_location("gateway_http_under_test", GATEWAY_PATH)
gateway = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gateway)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self.request_threads: set[threading.Thread] = set()
        self.request_threads_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request, client_address):
        current = threading.current_thread()
        with self.request_threads_lock:
            self.request_threads.add(current)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.request_threads_lock:
                self.request_threads.discard(current)

    def join_request_threads(self):
        with self.request_threads_lock:
            threads = tuple(self.request_threads)
        for worker in threads:
            worker.join(timeout=2)
        assert not any(worker.is_alive() for worker in threads)


@pytest.fixture
def upstream_and_gateway(monkeypatch):
    observed: list[tuple[str, str, dict[str, str]]] = []
    mode = {"response": "normal"}

    class UpstreamHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            observed.append((self.command, self.path, dict(self.headers.items())))
            if mode["response"] == "delay":
                time.sleep(0.35)
            if mode["response"] == "malformed":
                self.connection.sendall(b"malformed synthetic-upstream-secret\r\n\r\n")
                return
            if mode["response"] == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            body = b"fixture subscription body"
            response_status = {
                "status": 206,
                "http_error": 503,
            }.get(mode["response"], 200)
            self.send_response(response_status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Subscription-Userinfo", "upload=10; download=20")
            self.send_header("Profile-Title", "fixture profile")
            self.send_header("Profile-Update-Interval", "12")
            self.send_header("X-Private-Upstream", "must-not-pass")
            self.send_header("X-FWRouter-Diagnostic", "must-not-pass")
            self.send_header("Set-Cookie", "must-not-pass")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    upstream = _Server(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    monkeypatch.setattr(gateway, "UPSTREAM_HOST", "127.0.0.1")
    monkeypatch.setattr(gateway, "UPSTREAM_PORT", upstream.server_address[1])

    class TestHandler(gateway.Handler):
        upstream_timeout = 0.12

    proxy = _Server(("127.0.0.1", 0), TestHandler)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        yield proxy, mode, observed
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy.join_request_threads()
        upstream.shutdown()
        upstream.server_close()
        upstream.join_request_threads()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)
        assert not proxy_thread.is_alive()
        assert not upstream_thread.is_alive()


def _request(server, method: str, path: str, *, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=1)
    try:
        if headers is None:
            headers = {
                "Host": "public.example.test",
                "User-Agent": "gateway-contract-test",
                "Accept": "text/plain",
                "X-Forwarded-For": "198.51.100.19",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "subscription.example.test",
                "X-Private-Request": "must-not-forward",
                "Authorization": "Bearer synthetic-authorization-secret",
                "Cookie": "session=synthetic-cookie-secret",
                "X-Synthetic-Header": "synthetic-header-secret",
            }
        connection.request(
            method,
            path,
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def _request_head_wire(server, path: str):
    """Read HEAD response bytes without HTTPResponse's automatic HEAD body suppression."""
    sock = socket.create_connection(server.server_address, timeout=1)
    sock.settimeout(1)
    try:
        sock.sendall(
            (
                f"HEAD {path} HTTP/1.0\r\n"
                "Host: public.example.test\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        # No method is supplied, so HTTPResponse reads any bytes sent after headers.
        response = http.client.HTTPResponse(sock)
        response.begin()
        body = response.read(65536)
        if len(body) == 65536:
            body += response.read(1)
        return response.status, body, dict(response.getheaders())
    finally:
        sock.close()


def test_real_get_forwards_allowlisted_request_and_response_contract(
    upstream_and_gateway, capsys
):
    proxy, mode, observed = upstream_and_gateway
    mode["response"] = "status"

    status, body, headers = _request(
        proxy,
        "GET",
        "/s/synthetic-path-secret?token=synthetic-query-secret&format=plain",
    )

    assert status == 206
    assert body == b"fixture subscription body"
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Subscription-Userinfo"] == "upload=10; download=20"
    assert headers["Profile-Title"] == "fixture profile"
    assert headers["Profile-Update-Interval"] == "12"
    assert "X-Private-Upstream" not in headers
    assert "X-FWRouter-Diagnostic" not in headers
    assert "Set-Cookie" not in headers
    assert len(observed) == 1
    method, path, forwarded = observed[0]
    assert method == "GET"
    assert path == (
        "/s/synthetic-path-secret?token=synthetic-query-secret&format=plain"
    )
    assert forwarded["User-Agent"] == "gateway-contract-test"
    assert forwarded["Accept"] == "text/plain"
    assert forwarded["X-Forwarded-For"] == "198.51.100.19"
    assert forwarded["X-Forwarded-Proto"] == "https"
    assert forwarded["X-Forwarded-Host"] == "subscription.example.test"
    assert "X-Private-Request" not in forwarded
    assert "Authorization" not in forwarded
    assert "Cookie" not in forwarded
    assert "X-Synthetic-Header" not in forwarded
    logs = capsys.readouterr().err
    for sensitive in (
        "synthetic-path-secret",
        "synthetic-query-secret",
        "synthetic-authorization-secret",
        "synthetic-cookie-secret",
        "synthetic-header-secret",
        "127.0.0.1",
        "/s/",
    ):
        assert sensitive not in logs


def test_real_head_preserves_headers_without_response_body(upstream_and_gateway):
    proxy, _mode, observed = upstream_and_gateway
    status, body, headers = _request_head_wire(proxy, "/s/head-capability")
    assert status == 200
    assert body == b""
    assert headers["Content-Type"] == "text/plain; charset=utf-8"
    assert observed[0][0:2] == ("GET", "/s/head-capability")


def test_raw_head_helper_detects_body_bytes_from_a_misbehaving_server():
    class BodyOnHeadHandler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "7")
            self.end_headers()
            self.wfile.write(b"leaked!")

        def log_message(self, fmt, *args):
            pass

    server = _Server(("127.0.0.1", 0), BodyOnHeadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body, _headers = _request_head_wire(server, "/negative-control")
        assert status == 200
        assert body == b"leaked!"
    finally:
        server.shutdown()
        server.server_close()
        server.join_request_threads()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_missing_forwarded_headers_use_safe_defaults(upstream_and_gateway):
    proxy, _mode, observed = upstream_and_gateway
    status, _body, _headers = _request(
        proxy,
        "GET",
        "/s/default-header-capability",
        headers={"Host": "minimal.example.test"},
    )
    assert status == 200
    forwarded = observed[0][2]
    assert forwarded["X-Forwarded-Host"] == "minimal.example.test"
    assert forwarded["X-Forwarded-Proto"] == "https"
    assert forwarded["X-Forwarded-For"] == "127.0.0.1"


def test_head_upstream_failure_has_502_and_no_body(upstream_and_gateway):
    proxy, mode, _observed = upstream_and_gateway
    mode["response"] = "malformed"
    status, body, _headers = _request_head_wire(proxy, "/s/head-failure-capability")
    assert status == 502
    assert body == b""


def test_real_upstream_http_error_status_and_body_are_forwarded(upstream_and_gateway):
    proxy, mode, _observed = upstream_and_gateway
    mode["response"] = "http_error"
    status, body, headers = _request(proxy, "GET", "/s/http-error-capability")
    assert status == 503
    assert body == b"fixture subscription body"
    assert headers["Content-Type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize(
    "path",
    [
        "/not-a-subscription",
        "/s/bad%2Ftoken",
        "/api/v2/xray/clients/not-a-uuid/subscription.txt",
    ],
)
def test_invalid_paths_are_rejected_without_upstream_request(upstream_and_gateway, path):
    proxy, _mode, observed = upstream_and_gateway
    status, body, _headers = _request(proxy, "GET", path)
    assert status == 404
    assert body == b""
    assert observed == []


def test_legacy_uuid_path_and_query_are_forwarded(upstream_and_gateway):
    proxy, _mode, observed = upstream_and_gateway
    client_id = "123e4567-e89b-42d3-a456-426614174000"
    status, body, _headers = _request(
        proxy, "GET", f"/api/v2/xray/clients/{client_id}/subscription.txt?download=1"
    )
    assert status == 200
    assert body == b"fixture subscription body"
    assert observed[0][1] == (
        f"/api/v2/xray/clients/{client_id}/subscription.txt?download=1"
    )


@pytest.mark.parametrize("mode_name", ["delay", "malformed", "disconnect"])
def test_upstream_timeout_and_malformed_response_map_to_bounded_generic_502(
    upstream_and_gateway, mode_name, capsys
):
    proxy, mode, _observed = upstream_and_gateway
    mode["response"] = mode_name
    started = time.monotonic()
    status, body, _headers = _request(proxy, "GET", "/s/synthetic-capability")
    elapsed = time.monotonic() - started

    assert status == 502
    assert body == b"upstream unavailable\n", repr(body)
    assert elapsed < 0.8
    logs = capsys.readouterr().err
    assert "synthetic-capability" not in logs
    assert "synthetic-upstream-secret" not in body.decode()


def test_upstream_refusal_maps_to_generic_502_without_request_data(monkeypatch):
    reserved_non_listener = socket.socket()
    reserved_non_listener.bind(("127.0.0.1", 0))
    refused_port = reserved_non_listener.getsockname()[1]
    monkeypatch.setattr(gateway, "UPSTREAM_HOST", "127.0.0.1")
    monkeypatch.setattr(gateway, "UPSTREAM_PORT", refused_port)

    class TestHandler(gateway.Handler):
        upstream_timeout = 0.12

    proxy = _Server(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        status, body, _headers = _request(proxy, "GET", "/s/refusal-capability")
        assert status == 502
        assert body == b"upstream unavailable\n"
    finally:
        reserved_non_listener.close()
        proxy.shutdown()
        proxy.server_close()
        proxy.join_request_threads()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_proxy_failure_logs_never_include_request_path_or_client(capsys, upstream_and_gateway):
    proxy, mode, _observed = upstream_and_gateway
    mode["response"] = "malformed"
    status, _body, _headers = _request(proxy, "GET", "/s/log-secret-capability?token=log-secret")
    logs = capsys.readouterr().err
    assert status == 502
    assert "log-secret" not in logs
    assert "/s/" not in logs
    assert "127.0.0.1" not in logs
