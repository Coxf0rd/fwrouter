from __future__ import annotations

import json
import os
import signal
import threading
import time

import pytest

from .http_support import http_json
from .xray_support import loaded_identities


@pytest.mark.l7
def test_api_worker_sigkill_during_native_reload_preserves_owned_process_state(acceptance_stack):
    """Kill the actual uvicorn worker at an apply barrier; no checkpoint recovery is claimed."""
    stack = acceptance_stack
    worker = stack["worker"]
    stack["native"].hold_next_reload()
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-kill-barrier", "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    assert stack["native"].reload_entered.wait(20), "actual Xray reload did not reach the bounded pause barrier"
    try:
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)
        assert worker.returncode == -signal.SIGKILL
    finally:
        stack["native"].release_reload()

    deadline = time.monotonic() + 12
    loaded: list[tuple[str, str]] = []
    active_pairs: list[tuple[str, str]] = []
    poll_wait = threading.Event()
    while time.monotonic() < deadline:
        try:
            loaded = loaded_identities(stack["native"])
            active = json.loads((stack["state"] / "xray" / "config.json").read_text(encoding="utf-8"))
            active_clients = active["inbounds"][1]["settings"]["clients"]
            active_pairs = sorted((str(item.get("id") or ""), str(item.get("email") or "")) for item in active_clients)
            if loaded == active_pairs:
                break
        except Exception:
            poll_wait.wait(0.1)
    assert loaded == active_pairs and loaded, {"loaded": loaded, "active": active_pairs}

    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    code, listing = http_json(f"{restarted_api}/xray/clients")
    assert code == 200 and listing.get("ok") is True, listing
    listed_pairs = sorted((str(item.get("client_id") or ""), str(item.get("email") or ""))
                          for item in listing.get("data", {}).get("clients", []))
    assert set(active_pairs).issubset(set(listed_pairs)), {"active": active_pairs, "listed": listed_pairs}
