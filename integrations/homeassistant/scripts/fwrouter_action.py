#!/usr/bin/env python3
"""FWRouter HA actions with post-apply verification."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


BASE_URL = "http://127.0.0.1:5500/api/v2"
TIMEOUT_SECONDS = 60
POLL_SECONDS = 1.5
REQUESTED_BY = "external_client:homeassistant"


def management_context(action: str) -> dict:
    return {
        "source_type": "external_client",
        "client_name": "homeassistant",
        "channel": "local_api",
        "action": action,
    }


def request_json(method: str, path: str, payload: dict | None = None) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(parsed.get("error") or parsed)
    return parsed


def routing() -> dict:
    return request_json("GET", "/routing/global")["data"]["routing"]


def vpn_auto_state() -> dict:
    return request_json("GET", "/selector/vpn-auto/state")["data"]


def router_summary() -> dict:
    return request_json("GET", "/ui/router-summary")["data"].get("router", {})


def servers_by_id() -> dict[str, dict]:
    rows = request_json("GET", "/servers")["data"].get("servers", [])
    return {row.get("server_id"): row for row in rows if row.get("server_id")}


def require_usable_auto_server(server_id: str) -> None:
    vpn_auto = vpn_auto_state().get("vpn_auto") or {}
    enabled_ids = vpn_auto.get("enabled_candidate_ids") or []
    if server_id in enabled_ids:
        return

    server = servers_by_id().get(server_id)
    if not server:
        raise RuntimeError(f"server is not present in FWRouter vpn-auto candidates: {server_id}")
    preferences = server.get("preferences") or {}
    if preferences.get("vpn_auto") is not True:
        raise RuntimeError(f"server is not enabled for vpn-auto: {server_id}")
    ping = server.get("ping") or {}
    if ping.get("status") != "success" or ping.get("last_ping_ms") is None:
        details = ping.get("error_message") or ping.get("error_code") or "no successful cached ping"
        raise RuntimeError(f"server has no successful ping: {server_id}; {details}")


def wait_until(label: str, predicate) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last is True:
            return
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"{label} was not confirmed by FWRouter API; last={last!r}")


def set_mode(mode: str) -> None:
    if mode not in {"direct", "selective", "vpn"}:
        raise ValueError(f"unsupported mode: {mode}")
    request_json(
        "POST",
        "/routing/global",
        {
            "mode": mode,
            "requested_by": REQUESTED_BY,
            "management_context": management_context(f"set_global_mode:{mode}"),
        },
    )

    def confirmed():
        state = routing()
        return (
            state.get("desired_mode") == mode
            and state.get("applied_mode") == mode
            and state.get("apply_state") in {"clean", "applied", None}
        ) or state

    wait_until(f"mode {mode}", confirmed)


def set_server(server_key: str) -> None:
    server_id = str(server_key or "").strip()
    if not server_id:
        raise ValueError("server_id is required")
    require_usable_auto_server(server_id)
    request_json(
        "POST",
        "/routing/global/fixed-server",
        {
            "server_id": server_id,
            "confirm_switch": True,
            "requested_by": REQUESTED_BY,
            "management_context": management_context("set_global_fixed_server"),
        },
    )

    def confirmed():
        state = routing()
        return (
            state.get("desired_fixed_server_id") == server_id
            and state.get("applied_fixed_server_id") == server_id
            and state.get("apply_state") in {"clean", "applied", None}
        ) or state

    wait_until(f"fixed server {server_id}", confirmed)


def clear_server() -> None:
    request_json(
        "DELETE",
        (
            "/routing/global/fixed-server"
            "?confirm_switch=true"
            f"&requested_by={REQUESTED_BY}"
            "&management_action=clear_global_fixed_server"
            "&management_client_name=homeassistant"
            "&management_channel=local_api"
        ),
    )

    def confirmed():
        state = routing()
        return (
            state.get("desired_fixed_server_id") is None
            and state.get("applied_fixed_server_id") is None
            and state.get("server_mode") == "auto"
            and state.get("apply_state") in {"clean", "applied", None}
        ) or state

    wait_until("auto server mode", confirmed)


def switch_best() -> None:
    response = request_json(
        "POST",
        "/selector/vpn-auto/switch",
        {
            "confirm_switch": True,
            "requested_by": REQUESTED_BY,
            "management_context": management_context("switch_best_vpn_auto_server"),
        },
    )
    selector = response.get("data", {}).get("selector") or {}
    transition = selector.get("auto_transition") or {}
    effective = selector.get("effective_route") or {}
    outcome = selector.get("selection_outcome") or transition.get("outcome")

    if selector.get("ok") is not True:
        raise RuntimeError(f"best-server switch failed: {selector.get('error_code') or outcome or 'unknown'}")
    if outcome == "noop":
        if transition.get("changed") is not False:
            raise RuntimeError("best-server no-op was not confirmed by selector readback")
    elif outcome == "selected":
        if selector.get("applied") is not True or transition.get("changed") is not True:
            raise RuntimeError("best-server change was not confirmed by selector readback")
    else:
        raise RuntimeError(f"best-server switch was not confirmed: {outcome or 'unknown'}")

    expected_id = transition.get("active_after_id") or selector.get("selected_server_id")
    if not expected_id:
        raise RuntimeError("best-server switch returned no confirmed logical server")
    reason = transition.get("reason_code") or "unknown"
    source = transition.get("origin") or "unknown"
    attribution = transition.get("actor_attribution") or "unknown"

    def confirmed():
        state = vpn_auto_state()
        vpn_auto = state.get("vpn_auto") if isinstance(state.get("vpn_auto"), dict) else state
        active_id = vpn_auto.get("active_auto_server_id")
        if (
            active_id != expected_id
            or vpn_auto.get("active_auto_server_valid") is not True
            or vpn_auto.get("config_consistent") is not True
        ):
            return False

        current = router_summary()
        current_mode = str(current.get("server_mode") or "").lower()
        if current_mode == "fixed":
            return (
                current.get("current_server_source") == "manual"
                and current.get("current_server_id") == effective.get("fixed_server_id")
            )
        return current.get("current_server_id") == expected_id

    wait_until("best auto server switch", confirmed)
    current = router_summary()
    logical_name = (
        transition.get("active_after_name")
        or transition.get("selected_server_name")
        or "unknown"
    )
    effective_change = effective.get("changed")
    effective_status = (
        "changed"
        if effective_change is True
        else "unchanged"
        if effective_change is False
        else "unconfirmed"
    )
    print(
        f"auto_transition={outcome}; effective_route_change={effective_status}; "
        f"logical_server={logical_name}; reason={reason}; "
        f"source={source}; attribution={attribution}"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3 and not (len(argv) == 2 and argv[1] in {"auto", "best"}):
        print("usage: fwrouter_action.py mode <direct|selective|vpn>|server <key>|auto|best", file=sys.stderr)
        return 2
    try:
        action = argv[1]
        if action == "mode":
            set_mode(argv[2])
        elif action == "server":
            set_server(argv[2])
        elif action == "auto":
            clear_server()
        elif action == "best":
            switch_best()
        else:
            raise ValueError(f"unsupported action: {action}")
    except (TimeoutError, ValueError, urllib.error.URLError, RuntimeError) as exc:
        print(f"FWRouter action failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
