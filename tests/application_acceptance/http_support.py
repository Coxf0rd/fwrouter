from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def http_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 3.0) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(2 * 1024 * 1024))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read(2 * 1024 * 1024))
