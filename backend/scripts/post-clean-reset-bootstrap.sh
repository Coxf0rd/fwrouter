#!/bin/sh
set -eu

BASE_URL="${FWROUTER_BASE_URL:-http://127.0.0.1:5000/api/v2}"
PYTHON_BIN="${FWROUTER_PYTHON_BIN:-/opt/fwrouter-api/.venv/bin/python}"
REQUESTED_BY="${FWROUTER_REQUESTED_BY:-post-clean-reset-bootstrap}"
DISCOVER_DOCKER="${FWROUTER_DISCOVER_DOCKER:-true}"
DISCOVER_HOST="${FWROUTER_DISCOVER_HOST:-true}"
DISCOVER_TAILSCALE="${FWROUTER_DISCOVER_TAILSCALE:-true}"
DISCOVER_XRAY="${FWROUTER_DISCOVER_XRAY:-true}"
INCLUDE_ALL_TAILSCALE_PEERS="${FWROUTER_INCLUDE_ALL_TAILSCALE_PEERS:-false}"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Python interpreter not found: $PYTHON_BIN" >&2
    exit 1
fi

export FWROUTER_BASE_URL="$BASE_URL"
export FWROUTER_REQUESTED_BY="$REQUESTED_BY"
export FWROUTER_DISCOVER_DOCKER
export FWROUTER_DISCOVER_HOST
export FWROUTER_DISCOVER_TAILSCALE
export FWROUTER_DISCOVER_XRAY
export FWROUTER_INCLUDE_ALL_TAILSCALE_PEERS

"$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.environ["FWROUTER_BASE_URL"]
REQUESTED_BY = os.environ["FWROUTER_REQUESTED_BY"]


def _as_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def api_get(path: str) -> dict:
    request = urllib.request.Request(f"{BASE_URL}{path}")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


def api_post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


def show(title: str, payload: dict) -> None:
    print()
    print(f"=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def request_or_die(title: str, fn) -> dict:
    try:
        payload = fn()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print()
        print(f"=== {title} ===")
        print(body)
        raise
    except Exception as exc:  # noqa: BLE001
        print()
        print(f"=== {title} ===")
        print(f"FAILED: {exc}")
        raise

    show(title, payload)
    return payload


def main() -> int:
    request_or_die("Health", lambda: api_get("/health"))

    request_or_die(
        "System Subjects Sync",
        lambda: api_post(
            "/system-subjects/sync",
            {
                "requested_by": REQUESTED_BY,
                "run_now": True,
                "discover_docker": _as_bool("FWROUTER_DISCOVER_DOCKER", False),
                "discover_host": _as_bool("FWROUTER_DISCOVER_HOST", True),
            },
        ),
    )

    request_or_die(
        "Client Subjects Sync",
        lambda: api_post(
            "/subjects/sync",
            {
                "requested_by": REQUESTED_BY,
                "run_now": True,
                "discover_docker": False,
                "discover_host": False,
                "discover_tailscale": _as_bool("FWROUTER_DISCOVER_TAILSCALE", True),
                "discover_xray": _as_bool("FWROUTER_DISCOVER_XRAY", True),
                "include_all_tailscale_peers": _as_bool("FWROUTER_INCLUDE_ALL_TAILSCALE_PEERS", False),
                "lan_clients": [],
                "tailscale_nodes": [],
                "host_services": [],
            },
        ),
    )

    request_or_die("System Subjects", lambda: api_get("/system-subjects"))
    request_or_die("Subjects", lambda: api_get("/subjects?limit=200"))
    request_or_die("Runtime", lambda: api_get("/runtime"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTP error: {exc.code}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"Unhandled error: {exc}", file=sys.stderr)
        raise SystemExit(1)
PY
