#!/bin/sh
set -eu

HOST="${1:?host is required}"
PORT="${2:?port is required}"
TIMEOUT="${3:-60}"
LABEL="${4:-port}"

python3 - "$HOST" "$PORT" "$TIMEOUT" "$LABEL" <<'PY'
import socket
import sys
import time

host, port_text, timeout_text, label = sys.argv[1:5]
port = int(port_text)
timeout = float(timeout_text)
deadline = time.monotonic() + timeout
last_error = None

while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            raise SystemExit(0)
    except OSError as exc:
        last_error = exc
        time.sleep(0.5)

message = f"fwrouter wait-port: {label} not ready on {host}:{port}"
if last_error is not None:
    message = f"{message}: {last_error}"
print(message, file=sys.stderr)
raise SystemExit(1)
PY
