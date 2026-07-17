#!/bin/sh
set -eu

if [ $# -lt 1 ]; then
    echo "Usage: $0 <subscription-url>" >&2
    exit 1
fi

BASE_URL="${FWROUTER_BASE_URL:-http://127.0.0.1:5000/api/v2}"
PYTHON_BIN="${FWROUTER_PYTHON_BIN:-/opt/fwrouter-api/.venv/bin/python}"
REQUESTED_BY="${FWROUTER_REQUESTED_BY:-post-reset-subscription-refresh}"
SUBSCRIPTION_URL="$1"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Python interpreter not found: $PYTHON_BIN" >&2
    exit 1
fi

export FWROUTER_BASE_URL="$BASE_URL"
export FWROUTER_REQUESTED_BY="$REQUESTED_BY"
export FWROUTER_SUBSCRIPTION_URL="$SUBSCRIPTION_URL"

"$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.environ["FWROUTER_BASE_URL"]
REQUESTED_BY = os.environ["FWROUTER_REQUESTED_BY"]
SUBSCRIPTION_URL = os.environ["FWROUTER_SUBSCRIPTION_URL"]


def api_get(path: str) -> dict:
    request = urllib.request.Request(f"{BASE_URL}{path}")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


def api_post(path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=(json.dumps(payload).encode() if payload is not None else b""),
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
        "Save Subscription URL",
        lambda: api_post(
            "/subscription",
            {
                "url": SUBSCRIPTION_URL,
                "metadata": {
                    "requested_by": REQUESTED_BY,
                    "source": "post-reset-subscription-refresh",
                },
            },
        ),
    )
    request_or_die("Refresh Subscription", lambda: api_post("/subscription/refresh"))
    request_or_die("Subscription State", lambda: api_get("/subscription"))
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
