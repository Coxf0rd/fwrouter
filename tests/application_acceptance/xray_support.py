from __future__ import annotations

import json
import time

from .http_support import http_json


def await_job(api: str, response: dict, *, timeout: float = 75) -> dict:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    job_id = str(job.get("job_id") or "")
    assert job_id, f"API did not return the accepted job: {response!r}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code, current = http_json(f"{api}/jobs/{job_id}", timeout=3)
        assert code == 200 and current.get("ok") is True, current
        current_data = current.get("data") if isinstance(current.get("data"), dict) else {}
        current_job = current_data.get("job") if isinstance(current_data.get("job"), dict) else {}
        if current_job.get("status") in {"success", "failed", "cancelled"}:
            return current_job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach a terminal status")


def loaded_identities(native) -> list[tuple[str, str]]:
    reply = native.rpc("api_inbound_users", {"tag": "vless-ws"})
    assert reply.get("ok") is True, reply
    payload = json.loads(reply["details"]["stdout"])
    users = payload.get("users")
    assert isinstance(users, list), payload
    result = []
    for user in users:
        account = user.get("account") if isinstance(user, dict) else None
        assert isinstance(account, dict), user
        assert account.get("_TypedMessage_") == "xray.proxy.vless.Account", account
        result.append((str(account.get("id") or ""), str(user.get("email") or "")))
    return sorted(result)
