from __future__ import annotations

import http.server
import json
import threading
from uuid import NAMESPACE_DNS, uuid5


class SyntheticSubscriptionSource:
    """Serve a mutable synthetic profile on a suite-owned loopback socket."""

    def __init__(self, payload: dict) -> None:
        self._lock = threading.Lock()
        self._body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self._request_count = 0
        source = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/synthetic-subscription":
                    self.send_error(404)
                    return
                with source._lock:
                    source._request_count += 1
                    body = source._body
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = False
        self._server.block_on_close = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}/synthetic-subscription"

    def replace(self, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        with self._lock:
            self._body = body

    def snapshot_requests(self) -> int:
        with self._lock:
            return self._request_count

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)
        assert not self._thread.is_alive(), "synthetic subscription source did not stop"


def synthetic_vless_profile(*, short_id_suffix: str = "", include_third_member: bool = False) -> dict:
    def outbound(index: int) -> dict:
        return {
            "tag": f"synthetic-member-{index}",
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": f"synthetic-member-{index}.example.invalid",
                "port": 443,
                "users": [{
                    "id": str(uuid5(NAMESPACE_DNS, f"pending-generation-member-{index}")),
                    "encryption": "none",
                    "flow": "xtls-rprx-vision",
                }],
            }]},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "serverName": f"synthetic-member-{index}.example.invalid",
                    "publicKey": "CQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "shortId": f"{index + 1:02x}" * 8 if not short_id_suffix else f"{index + 1:02x}{short_id_suffix}",
                    "fingerprint": "chrome",
                },
            },
        }

    count = 3 if include_third_member else 2
    return {
        "remarks": "acceptance-pending-generation-profile",
        "outbounds": [outbound(index) for index in range(count)] + [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "reject", "protocol": "blackhole"},
            {"tag": "dns", "protocol": "dns"},
        ],
        "routing": {"rules": [{"type": "field", "outboundTag": "direct"}]},
    }
