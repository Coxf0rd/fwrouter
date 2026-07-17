#!/usr/bin/env python3
from __future__ import annotations

import http.client
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LISTEN_HOST = "172.18.0.1"
LISTEN_PORT = 5055

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 5000

UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")

LEGACY_SUB_RE = re.compile(
    r"^/api/v2/xray/clients/([^/]+)/subscription\.txt$"
)

PUBLIC_SUB_RE = re.compile(
    r"^/s/([^/]+)$"
)


def _build_path(path: str, query: str) -> str:
    return path + (f"?{query}" if query else "")


def _resolve_upstream_path(raw_path: str) -> str | None:
    parsed = urlsplit(raw_path)
    path = parsed.path
    query = parsed.query

    public_match = PUBLIC_SUB_RE.match(path)
    if public_match:
        token = public_match.group(1)
        if not TOKEN_RE.match(token):
            return None
        return _build_path(path, query)

    legacy_match = LEGACY_SUB_RE.match(path)
    if legacy_match:
        client_id = legacy_match.group(1)

        # Real UUID clients keep the old endpoint.
        if UUID_RE.match(client_id):
            return _build_path(path, query)

    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "FWRouterXraySubGateway/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (
            self.address_string(),
            self.log_date_time_string(),
            fmt % args,
        ))

    def do_HEAD(self) -> None:
        self._proxy(send_body=False)

    def do_GET(self) -> None:
        self._proxy(send_body=True)

    def _proxy(self, *, send_body: bool) -> None:
        upstream_path = _resolve_upstream_path(self.path)
        if upstream_path is None:
            self.send_response(404)
            self.end_headers()
            return

        headers = {
            "Host": f"{UPSTREAM_HOST}:{UPSTREAM_PORT}",
            "User-Agent": self.headers.get("User-Agent", ""),
            "Accept": self.headers.get("Accept", "*/*"),
        }

        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=180)
            conn.request("GET", upstream_path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        except Exception as exc:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if send_body:
                self.wfile.write(f"upstream error: {exc}\n".encode("utf-8"))
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        self.send_response(resp.status)

        passthrough_headers = {
            "content-type",
            "subscription-userinfo",
            "profile-title",
            "profile-update-interval",
            "cache-control",
            "x-fwrouter-subscription-client",
            "x-fwrouter-detected-format",
            "x-fwrouter-nodes-count",
            "x-fwrouter-xray-clients-count",
            "x-fwrouter-handoff-count",
            "x-fwrouter-renderer",
        }

        for key, value in resp.getheaders():
            lower = key.lower()
            if lower in passthrough_headers:
                self.send_header(key, value)

        self.end_headers()

        if send_body:
            self.wfile.write(body)


def main() -> None:
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
