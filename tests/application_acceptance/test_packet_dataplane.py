from __future__ import annotations

import ipaddress
import ctypes
import json
import os
import selectors
import socket
import stat
import struct
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from .http_support import http_json
from .joined_support import await_core_job
from .test_core_provider_mihomo import _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source
from .xray_support import assert_mihomo_launch_matches_active


CLIENT_IP = "10.240.0.2"
ROUTER_WAN_IP = "198.18.240.1"
ENDPOINT_IP = "198.18.240.2"
SERVICE_VIP = "203.0.113.53"
CLIENT_CONTROL = "http://10.240.0.2:8081"
ENDPOINT_CONTROL = "http://198.18.240.2:8082"
CAPTURE_ROOT = Path("/tmp/fwrouter-packet-evidence")
TCP_CAPTURE_PATH = CAPTURE_ROOT / "tcp.pcap"
UDP_CAPTURE_PATH = CAPTURE_ROOT / "udp.pcap"
CAPTURE_SUMMARY_PATH = CAPTURE_ROOT / "appboundedpacket-summary.json"
CAPTURE_LIMIT = 512
TCP_SNAPLEN = 54
UDP_SNAPLEN = 42
CORE_NFT_TABLE = "fwrouter_v2"
ROUTER_GUARD_TABLE = "fwrouter_packet_guard"
MAX_CORE_NFT_JSON = 1024 * 1024


def _role_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("ascii") if payload is not None else None
    assert body is None or len(body) <= 256, "packet role request exceeded its fixed byte limit"
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            code = response.status
            raw = response.read(8193)
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read(8193)
    except (OSError, TimeoutError) as exc:
        raise AssertionError("packet role control request did not complete within its bound") from exc
    assert len(raw) <= 8192, "packet role response exceeded its fixed byte limit"
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("packet role response was not valid bounded JSON") from exc
    assert code == 200 and isinstance(value, dict), {"status": code, "shape": type(value).__name__}
    return value


def _pcap_counts(path: Path, expected_snaplen: int, expected_protocol: int,
                 expected_ports: set[int]) -> dict[tuple[str, str, str, int], int]:
    """Read only fixed IPv4 headers from a launcher-owned bounded live pcap."""
    try:
        root_info = CAPTURE_ROOT.lstat()
        capture_info = path.lstat()
        if (CAPTURE_ROOT.is_symlink() or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != 0 or root_info.st_mode & 0o777 != 0o700
                or path.is_symlink() or not stat.S_ISREG(capture_info.st_mode)
                or capture_info.st_uid != 0
                or capture_info.st_size > 24 + CAPTURE_LIMIT * (16 + expected_snaplen)):
            raise AssertionError("owned packet capture file violates its bounded path or size contract")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0
                    or opened.st_size > 24 + CAPTURE_LIMIT * (16 + expected_snaplen)):
                raise AssertionError("opened packet capture differs from its owned-file contract")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                raw = stream.read(24 + CAPTURE_LIMIT * (16 + expected_snaplen) + 1)
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        raise AssertionError("launcher-owned fixed packet capture is unavailable") from exc
    if len(raw) > 24 + CAPTURE_LIMIT * (16 + expected_snaplen) or len(raw) < 24:
        raise AssertionError("bounded packet capture has an invalid length")
    endians = {
        b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">",
    }
    endian = endians.get(raw[:4])
    if endian is None:
        raise AssertionError("packet capture pcap magic is unsupported")
    major, minor, _zone, _sigfigs, snaplen, linktype = struct.unpack_from(endian + "HHiiII", raw, 4)
    if major != 2 or minor != 4 or snaplen != expected_snaplen or linktype != 1:
        raise AssertionError("packet capture header differs from the fixed packet profile")

    counts: dict[tuple[str, str, str, int], int] = {}
    offset = 24
    records = 0
    while offset + 16 <= len(raw):
        _seconds, _fraction, captured, original = struct.unpack_from(endian + "IIII", raw, offset)
        offset += 16
        if captured > expected_snaplen or captured > original:
            raise AssertionError("packet capture record exceeds the header-only bound")
        if offset + captured > len(raw):
            break  # tcpdump may be writing the last packet while the live snapshot is read
        frame = raw[offset:offset + captured]
        offset += captured
        records += 1
        if records > CAPTURE_LIMIT:
            raise AssertionError("packet capture exceeded its packet-count limit")
        if linktype == 1:
            if len(frame) < 14 or frame[12:14] != b"\x08\x00":
                continue
            network_offset = 14
        else:
            raise AssertionError("packet capture link type differs from the fixed Ethernet contract")
        if len(frame) < network_offset + 24:
            raise AssertionError("captured IPv4 packet omitted fixed address and port headers")
        first = frame[network_offset]
        ihl = first & 0x0F
        if first >> 4 != 4 or ihl != 5:
            raise AssertionError("captured packet is not fixed IPv4 without options")
        protocol = frame[network_offset + 9]
        if protocol != expected_protocol:
            continue
        transport = network_offset + 20
        src = str(ipaddress.IPv4Address(frame[network_offset + 12:network_offset + 16]))
        dst = str(ipaddress.IPv4Address(frame[network_offset + 16:network_offset + 20]))
        _src_port, dst_port = struct.unpack_from("!HH", frame, transport)
        if dst_port not in expected_ports:
            continue
        if dst_port == 5301 and (src, dst, protocol) != (ROUTER_WAN_IP, ENDPOINT_IP, 6):
            raise AssertionError("captured VLESS flow differs from the fixed router-to-endpoint tuple")
        if dst_port in {9080, 9081, 5353} and dst != SERVICE_VIP:
            raise AssertionError("captured service flow differs from the fixed VIP")
        counts[(src, dst, "tcp" if protocol == 6 else "udp", dst_port)] = (
            counts.get((src, dst, "tcp" if protocol == 6 else "udp", dst_port), 0) + 1
        )
    return counts


def _packet_counts() -> dict[tuple[str, str, str, int], int]:
    tcp = _pcap_counts(TCP_CAPTURE_PATH, TCP_SNAPLEN, 6, {5301, 9080})
    udp = _pcap_counts(UDP_CAPTURE_PATH, UDP_SNAPLEN, 17, {9081, 5353})
    return {**tcp, **udp}


def _wait_packet_counts(predicate, *, timeout_seconds: float = 3.0) -> dict[tuple[str, str, str, int], int]:
    """Wait on owned pcap file-change events until the exact flow predicate holds."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise AssertionError("bounded packet capture event reader could not initialize")
    selector = selectors.DefaultSelector()
    try:
        if libc.inotify_add_watch(fd, os.fsencode(CAPTURE_ROOT), 0x00000002) < 0:
            raise AssertionError("bounded packet capture event reader could not watch its owned directory")
        selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = _packet_counts()
            if predicate(current):
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise AssertionError("owned packet capture did not satisfy its flow predicate before the deadline")
            while True:
                try:
                    events = os.read(fd, 4096)
                except BlockingIOError:
                    break
                if not events:
                    break
                offset = 0
                while offset + 16 <= len(events):
                    _watch, mask, _cookie, name_length = struct.unpack_from("iIII", events, offset)
                    if mask & 0x00004000:
                        raise AssertionError("owned packet capture inotify queue overflowed")
                    offset += 16 + name_length
    finally:
        selector.close()
        os.close(fd)


def _core_counter_snapshot() -> dict[str, int | None]:
    """Read the real FWRouter-owned table counters from the router namespace."""
    try:
        result = subprocess.run(
            ("/usr/sbin/nft", "-j", "list", "table", "inet", CORE_NFT_TABLE),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError("FWRouter nft counter readback did not complete within its bound") from exc
    if (result.returncode or len(result.stdout) > MAX_CORE_NFT_JSON
            or len(result.stderr) > 4096):
        raise AssertionError("FWRouter nft counter readback failed or exceeded its fixed byte bound")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("FWRouter nft counter readback was not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
        raise AssertionError("FWRouter nft counter readback has an invalid top-level shape")

    counters: dict[str, list[int]] = {
        "direct_lan_forward": [], "vpn_tcp": [], "vpn_udp": [], "vpn_udp_handoff": [],
    }
    for item in payload["nftables"]:
        rule = item.get("rule") if isinstance(item, dict) else None
        if (not isinstance(rule, dict) or rule.get("family") != "inet"
                or rule.get("table") != CORE_NFT_TABLE):
            continue
        chain = rule.get("chain")
        comment = rule.get("comment")
        if not isinstance(chain, str) or not isinstance(comment, str):
            continue
        kind = None
        if chain == "fwrouter_direct" and comment == "global direct path":
            kind = "direct_lan_forward"
        elif chain == "fwrouter_vpn_full" and comment.startswith("fwrouter vpn mark tcp:5204"):
            kind = "vpn_tcp"
        elif chain == "fwrouter_vpn_full" and comment.startswith("fwrouter vpn mark udp:5205"):
            kind = "vpn_udp"
        elif (chain == "prerouting"
              and comment.startswith("fwrouter full-vpn tproxy handoff udp:5205")):
            kind = "vpn_udp_handoff"
        if kind is None:
            continue
        expr = rule.get("expr")
        matches = [entry["counter"] for entry in expr if isinstance(entry, dict)
                   and isinstance(entry.get("counter"), dict)] if isinstance(expr, list) else []
        if len(matches) != 1 or type(matches[0].get("packets")) is not int:
            raise AssertionError("FWRouter path rule has no unique packet counter")
        counters[kind].append(matches[0]["packets"])
    if (len(counters["direct_lan_forward"]) != 1 or len(counters["vpn_tcp"]) != 1
            or len(counters["vpn_udp"]) != 1 or len(counters["vpn_udp_handoff"]) > 1):
        raise AssertionError("FWRouter dataplane did not expose the exact terminal direct and global-VPN counters")
    return {
        "direct_lan_forward": counters["direct_lan_forward"][0],
        # Core has no packet counter on its input hook. These source-defined
        # Global VPN mode classifies into fwrouter_vpn_full; these are its
        # fixed full-VPN TCP REDIR and UDP TProxy mark counters.
        "vpn_tcp": counters["vpn_tcp"][0],
        "vpn_udp": counters["vpn_udp"][0],
        # The handoff rule exists only while global VPN/selective policy needs it.
        "vpn_udp_handoff": counters["vpn_udp_handoff"][0] if counters["vpn_udp_handoff"] else None,
    }


def _core_counter_delta(before: dict[str, int | None], after: dict[str, int | None]) -> dict[str, int]:
    if set(before) != {"direct_lan_forward", "vpn_tcp", "vpn_udp", "vpn_udp_handoff"} or set(after) != set(before):
        raise AssertionError("FWRouter counter snapshots do not match the fixed path contract")
    if before["vpn_udp_handoff"] is None and after["vpn_udp_handoff"] is None:
        udp_handoff_delta = 0
    elif type(before["vpn_udp_handoff"]) is int and type(after["vpn_udp_handoff"]) is int:
        udp_handoff_delta = after["vpn_udp_handoff"] - before["vpn_udp_handoff"]
    else:
        raise AssertionError("FWRouter UDP TProxy handoff counter appeared or disappeared during a packet phase")
    raw_delta = {name: after[name] - before[name] for name in ("direct_lan_forward", "vpn_tcp", "vpn_udp")}
    delta = {
        "direct_lan_forward": raw_delta["direct_lan_forward"],
        "vpn_lan_input": raw_delta["vpn_tcp"] + raw_delta["vpn_udp"],
        "vpn_tcp_classified": raw_delta["vpn_tcp"],
        "vpn_udp_classified": raw_delta["vpn_udp"],
        "vpn_udp_handoff_packets": udp_handoff_delta,
    }
    if any(value < 0 for value in delta.values()):
        raise AssertionError("FWRouter path counter decreased during a packet phase")
    return delta


def _router_guard_counter_snapshot() -> dict[str, dict[str, int]]:
    """Read fixed named drops from the launcher-owned guard in the router namespace."""
    try:
        result = subprocess.run(
            ("/usr/sbin/nft", "-j", "list", "table", "inet", ROUTER_GUARD_TABLE),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError("router egress guard counter readback did not complete within its bound") from exc
    if (result.returncode or len(result.stdout) > MAX_CORE_NFT_JSON or len(result.stderr) > 4096):
        raise AssertionError("router egress guard readback failed or exceeded its fixed byte bound")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("router egress guard counter readback was not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
        raise AssertionError("router egress guard counter readback has an invalid shape")
    wanted = {"router_stub_drop", "router_egress_drop", "router_forward_drop"}
    found: dict[str, list[dict[str, int]]] = {name: [] for name in wanted}
    for item in payload["nftables"]:
        counter = item.get("counter") if isinstance(item, dict) else None
        if (isinstance(counter, dict) and counter.get("table") == ROUTER_GUARD_TABLE
                and counter.get("name") in wanted and type(counter.get("packets")) is int
                and type(counter.get("bytes")) is int):
            found[counter["name"]].append({"packets": counter["packets"], "bytes": counter["bytes"]})
    if any(len(values) != 1 for values in found.values()):
        raise AssertionError("router guard did not expose each required fixed named packet counter exactly once")
    return {name: values[0] for name, values in found.items()}


def _router_guard_counter_delta(before: dict[str, dict[str, int]],
                                after: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    if set(before) != {"router_stub_drop", "router_egress_drop", "router_forward_drop"} or set(after) != set(before):
        raise AssertionError("router guard counter snapshots differ from the fixed fixture contract")
    delta = {name: {field: after[name][field] - before[name][field]
                    for field in ("packets", "bytes")} for name in before}
    if any(value < 0 for row in delta.values() for value in row.values()):
        raise AssertionError("router guard counter decreased during a packet phase")
    return delta


def _probe_router_egress_guards() -> dict[str, Any]:
    """Exercise fixed router output-deny targets and one LAN forward-deny target."""
    before = _router_guard_counter_snapshot()
    stub_send_error = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.sendto(b"fwrouter-packet-guard", ("127.0.0.11", 53))
        except OSError as exc:
            # The kernel may surface a synchronous denial. The later counter
            # delta remains mandatory and is the only accepted proof of the guard.
            stub_send_error = type(exc).__name__
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((ENDPOINT_IP, 65000))
        except OSError:
            pass
        else:
            raise AssertionError("fixed router egress negative TCP probe unexpectedly connected")
    client_forward = _role_json(f"{CLIENT_CONTROL}/probe/router-forward-leak", method="POST")
    assert client_forward.get("ok") is True and client_forward.get("blocked") is True, {
        "router_forward_probe": "fixed client-to-endpoint negative connect was not blocked"
    }
    after = _router_guard_counter_snapshot()
    delta = _router_guard_counter_delta(before, after)
    if any(delta[name]["packets"] <= 0 or delta[name]["bytes"] <= 0
           for name in ("router_stub_drop", "router_egress_drop", "router_forward_drop")):
        raise AssertionError("fixed router output/forward negative probes did not increment all owned drop counters")
    return {**delta, "router_stub_send_oserror": stub_send_error}


def _flow_delta(before: dict[tuple[str, str, str, int], int],
                after: dict[tuple[str, str, str, int], int],
                flow: tuple[str, str, str, int]) -> int:
    return after.get(flow, 0) - before.get(flow, 0)


def _endpoint_observations() -> dict[str, Any]:
    response = _role_json(f"{ENDPOINT_CONTROL}/observations")
    assert set(response) == {"counts", "peers", "last_peers"}, {"keys": sorted(response)}
    assert (isinstance(response.get("counts"), dict) and isinstance(response.get("peers"), dict)
            and isinstance(response.get("last_peers"), dict)), {
        "shape": "invalid observations"
    }
    return response


def _mihomo_packet_path_proof(stack: dict[str, Any]) -> dict[str, Any]:
    """Return bounded semantic Mihomo facts without exposing config or proxy identifiers."""
    try:
        config_path = stack["state"] / "generated" / "mihomo" / "config.yaml"
        info = config_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or config_path.is_symlink() or info.st_size > 262144):
            return {"available": False, "reason": "active_config_bounds"}
        config = yaml.safe_load(config_path.read_bytes())
        if not isinstance(config, dict):
            return {"available": False, "reason": "active_config_shape"}
        listeners = config.get("listeners") if isinstance(config.get("listeners"), list) else []
        full_udp = [row for row in listeners if isinstance(row, dict)
                    and row.get("name") == "fwrouter-full-tproxy"
                    and row.get("type") == "tproxy" and row.get("port") == 5205
                    and row.get("rule") == "fwrouter-full-vpn" and row.get("udp") is True]
        proxies = config.get("proxies") if isinstance(config.get("proxies"), list) else []
        vless = [row for row in proxies if isinstance(row, dict) and row.get("type") == "vless"]
        assert_mihomo_launch_matches_active(stack["native"], config_path)
        code, response = http_json(f"{stack['api']}/mihomo")
        data = response.get("data") if isinstance(response, dict) else None
        mihomo = data.get("mihomo") if isinstance(data, dict) else None
        details = mihomo.get("details") if isinstance(mihomo, dict) else None
        selectors = details.get("selectors") if isinstance(details, dict) else None
        selectors = selectors if isinstance(selectors, dict) else {}
        transparent = details.get("transparent_runtime") if isinstance(details, dict) else None
        transparent = transparent if isinstance(transparent, dict) else {}
        count = transparent.get("transparent_udp_sessions_count")
        if type(count) is not int or count < 0 or count > 4096:
            count = None
        current_auto = selectors.get("vpn_auto_now")
        return {
            "available": code == 200 and isinstance(mihomo, dict),
            "native_loaded_config_matches_active": True,
            "runtime_running": isinstance(mihomo, dict) and mihomo.get("runtime_state") == "running",
            "vpn_global_selects_vpn_auto": selectors.get("vpn_global_now") == "vpn-auto",
            "vpn_auto_selects_non_direct": isinstance(current_auto, str) and current_auto.upper() != "DIRECT",
            "full_vpn_udp_listener_count": len(full_udp),
            "vless_proxy_count": len(vless),
            "vless_udp_enabled_count": sum(row.get("udp") is True for row in vless),
            "transparent_udp_sessions_count": count,
            "transparent_udp_session_materialized": transparent.get("transparent_udp_session_materialized") is True,
        }
    except Exception as exc:
        return {"available": False, "error_class": type(exc).__name__[:64]}


def _probe_phase(stack: dict[str, Any], expected_peer: str, label: str,
                 core_before: dict[str, int]) -> dict[str, Any]:
    before = _endpoint_observations()
    tcp = _role_json(f"{CLIENT_CONTROL}/probe/tcp", method="POST")
    udp = _role_json(f"{CLIENT_CONTROL}/probe/udp", method="POST")
    dns = _role_json(f"{CLIENT_CONTROL}/probe/dns", method="POST")
    negative = _role_json(f"{CLIENT_CONTROL}/probe/dns-negative", method="POST")
    leak = _role_json(f"{CLIENT_CONTROL}/probe/leak", method="POST")
    after = _endpoint_observations()
    try:
        core_counters = _core_counter_delta(core_before, _core_counter_snapshot())
    except Exception as exc:
        core_counters = {"available": False, "error_class": type(exc).__name__[:64]}
    diagnostic = {
        "phase": label,
        "endpoint_observation_delta": {
            kind: {"count": after["counts"].get(kind, -1) - before["counts"].get(kind, 0),
                  "last_peer": after["last_peers"].get(kind)}
            for kind in ("http", "udp", "dns")
        },
        "core_counter_delta": core_counters,
        "mihomo_path": _mihomo_packet_path_proof(stack),
    }
    assert tcp.get("ok") is True and tcp.get("peer") == expected_peer, {**diagnostic, "service": "tcp"}
    assert udp.get("ok") is True and udp.get("peer") == SERVICE_VIP, {**diagnostic, "service": "udp"}
    assert dns.get("ok") is True and dns.get("peer") == SERVICE_VIP and dns.get("answer") == SERVICE_VIP, {
        **diagnostic, "service": "dns"
    }
    assert negative.get("ok") is True and negative.get("rcode") == "NXDOMAIN", {
        **diagnostic, "service": "dns-negative"
    }
    assert leak.get("ok") is True and leak.get("blocked") is True, {**diagnostic, "service": "egress-guard"}
    for kind in ("http", "udp", "dns"):
        assert after["counts"].get(kind, -1) == before["counts"].get(kind, 0) + 1, {
            **diagnostic, "service": kind,
            "counts": {"before": before["counts"], "after": after["counts"]},
        }
        assert after["last_peers"].get(kind) == expected_peer, {
            **diagnostic, "service": kind, "expected_peer": expected_peer,
        }
    assert leak.get("stub_drop_packets", 0) > 0 and leak.get("reserved_drop_packets", 0) > 0, {
        "phase": label, "service": "egress-guard"
    }
    router_guard = _probe_router_egress_guards()
    return {"observations": after, "core_counters": core_counters,
            "mihomo_path": diagnostic["mihomo_path"], "leak": {
        "stub_drop_packets": leak["stub_drop_packets"],
        "reserved_drop_packets": leak["reserved_drop_packets"],
    }, "router_guard": router_guard}


def _set_health_available(available: bool) -> None:
    result = _role_json(f"{ENDPOINT_CONTROL}/health", method="POST", payload={"available": available})
    assert result.get("available") is available, {"health_control": "unexpected response"}


def _apply_global_mode(api: str, mode: str) -> dict[str, Any]:
    code, accepted = http_json(f"{api}/routing/global", method="POST",
                               payload={"mode": mode, "requested_by": "hosted-packet-acceptance", "run_now": True})
    assert code == 200 and accepted.get("ok") is True, {"mode": mode, "accepted": accepted.get("ok")}
    job = await_core_job(api, accepted)
    assert job.get("status") == "success", {"mode": mode, "job_status": job.get("status")}
    code, projection = http_json(f"{api}/routing/global")
    assert code == 200 and projection.get("ok") is True
    routing = projection["data"]["routing"]
    enforcement = projection["data"]["runtime_enforcement"]
    assert routing.get("applied_mode") == mode and enforcement.get("live_global_mode") == mode, {
        "mode": mode, "applied_mode": routing.get("applied_mode"),
        "live_global_mode": enforcement.get("live_global_mode"),
    }
    assert enforcement.get("active_mode_matches_intent") is True
    return projection["data"]


def _assert_packet_runtime(stack: dict, *, mode: str, policy_required: bool) -> None:
    active_config = stack["state"] / "generated" / "mihomo" / "config.yaml"
    assert_mihomo_launch_matches_active(stack["native"], active_config)
    code, mihomo = http_json(f"{stack['api']}/mihomo")
    assert code == 200 and mihomo.get("ok") is True
    state = mihomo["data"]["mihomo"]
    details = state.get("details") or {}
    selectors = details.get("selectors") or {}
    selected_global = selectors.get("vpn_global_now")
    allowed_global = {"vpn-auto"} if mode == "vpn" else {"vpn-auto", "DIRECT"}
    targets = selectors.get("vpn_global_targets") or []
    assert (state.get("runtime_state") == "running" and selected_global in allowed_global
            and selected_global in targets), {
        "runtime_state": state.get("runtime_state"), "vpn_global_now": selectors.get("vpn_global_now")
    }
    manifest_path = stack["state"] / "generated" / "dataplane" / "applied-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("summary", {}).get("requires_vpn_policy_routing") is policy_required, {
        "mode": mode, "requires_vpn_policy_routing": manifest.get("summary", {}).get("requires_vpn_policy_routing")
    }
    assert manifest.get("routing_global_state", {}).get("desired_mode") == mode, {
        "mode": mode, "manifest_mode": manifest.get("routing_global_state", {}).get("desired_mode")
    }


def _write_capture_summary(phases: dict[str, Any]) -> None:
    summary = json.dumps({"schema": "fwrouter-packet-test-flows/v1", "phases": phases},
                         sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(summary) > 4096:
        raise AssertionError("packet flow summary exceeded its fixed byte limit")
    fd = os.open(CAPTURE_SUMMARY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(summary)
        stream.flush()
        os.fsync(stream.fileno())


@pytest.mark.packet
def test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks(acceptance_stack):
    stack = acceptance_stack
    profile = stack["profile"]
    bridge, native, api = stack["provider_bridge"], stack["native"], stack["api"]
    assert profile.get("profile") == "hosted-kernel-packet"
    assert getattr(bridge, "packet_endpoint", False) is True
    provider_target = urlsplit(bridge.current_config.get("connection_url", ""))
    assert (provider_target.hostname, provider_target.port) == (ENDPOINT_IP, 5301), {
        "provider_target": "does not match the fixed fixture endpoint"
    }

    client_ready = _role_json(f"{CLIENT_CONTROL}/ready")
    endpoint_ready = _role_json(f"{ENDPOINT_CONTROL}/ready")
    assert client_ready.get("ready") is True and client_ready.get("role") == "client"
    assert endpoint_ready.get("ready") is True and endpoint_ready.get("role") == "endpoint"
    assert endpoint_ready.get("sockets") == {
        "xray_tcp_5301": True, "http_tcp_9080": True, "control_tcp_8082": True,
        "udp_echo_9081": True, "dns_udp_5353": True,
    }
    xray = endpoint_ready.get("xray")
    assert isinstance(xray, dict) and xray.get("running") is True
    assert xray.get("binary_sha256") == profile.get("xray", {}).get("sha256")

    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    code, enabled = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                              payload={"action": "enable"})
    assert code == 200 and enabled.get("ok") is True and enabled.get("data", {}).get("accepted") is True
    enable_job = await_core_job(api, enabled)
    assert enable_job.get("status") == "success"
    from fwrouter_api.services.provider_managed import binding_for
    binding = binding_for(source_ref)
    logical_id = binding["logical_server_id"]
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    code, exclusive = http_json(f"{api}/subscription/sources/{source_ref}/vpn-auto-exclusive", method="POST",
                                payload={"enabled": True})
    assert code == 200 and exclusive.get("ok") is True and exclusive.get("data", {}).get("accepted") is True
    assert await_core_job(api, exclusive).get("status") == "success"
    assert get_vpn_auto_exclusive_source_ref() == source_ref

    _set_health_available(True)
    _apply_global_mode(api, "direct")
    _assert_packet_runtime(stack, mode="direct", policy_required=False)
    capture_baseline = _packet_counts()
    core_before_direct = _core_counter_snapshot()
    direct_phase = _probe_phase(stack, CLIENT_IP, "direct-before-vpn", core_before_direct)
    after_direct = _wait_packet_counts(lambda counts: all(
        _flow_delta(capture_baseline, counts, flow) > 0 for flow in (
            (CLIENT_IP, SERVICE_VIP, "tcp", 9080),
            (CLIENT_IP, SERVICE_VIP, "udp", 9081),
            (CLIENT_IP, SERVICE_VIP, "udp", 5353),
        )))
    core_direct = direct_phase["core_counters"]
    assert core_direct["direct_lan_forward"] > 0, {"phase": "direct-before-vpn", "core_counters": core_direct}
    direct_flows = {
        "tcp_9080": _flow_delta(capture_baseline, after_direct, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(capture_baseline, after_direct, (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(capture_baseline, after_direct, (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
    }
    assert all(count > 0 for count in direct_flows.values()), {"phase": "direct-before-vpn", "flows": direct_flows}

    _apply_global_mode(api, "vpn")
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    before_vpn = _packet_counts()
    core_before_vpn = _core_counter_snapshot()
    vpn_phase = _probe_phase(stack, SERVICE_VIP, "vpn", core_before_vpn)
    after_vpn = _wait_packet_counts(lambda counts:
        _flow_delta(before_vpn, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    core_vpn = vpn_phase["core_counters"]
    assert (core_vpn["vpn_tcp_classified"] > 0 and core_vpn["vpn_udp_classified"] > 0
            and core_vpn["vpn_udp_handoff_packets"] > 0), {
        "phase": "vpn", "core_counters": core_vpn,
    }
    vpn_flow = _flow_delta(before_vpn, after_vpn, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301))
    vpn_direct_leaks = {
        "tcp_9080": _flow_delta(before_vpn, after_vpn, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(before_vpn, after_vpn, (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(before_vpn, after_vpn, (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
    }
    assert vpn_flow > 0 and all(count == 0 for count in vpn_direct_leaks.values()), {
        "phase": "vpn", "vless_endpoint_packets": vpn_flow, "direct_client_leaks": vpn_direct_leaks,
    }

    code, policy = http_json(f"{api}/subscription/sources/{source_ref}/provider/configuration",
                             method="POST", payload={"allow_automatic_member_switch": False})
    assert code == 200 and policy.get("ok") is True
    _set_health_available(False)
    bridge.calls.clear()
    code, first = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                            payload={"logical_server_id": logical_id, "decision_id": "packet-health-failure-1",
                                     "timeout_ms": 1000, "allow_switch": True}, timeout=90)
    assert code == 200 and first.get("provider_recovery") is True and first.get("provider_confirmation") == 1
    code, second = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                             payload={"logical_server_id": logical_id, "decision_id": "packet-health-failure-2",
                                      "timeout_ms": 1000, "allow_switch": True}, timeout=90)
    assert code == 200 and second.get("status") == "provider_auto_switch_disabled"
    code, direct = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                             payload={"logical_server_id": logical_id, "decision_id": "packet-health-failure-3",
                                      "timeout_ms": 1000, "allow_switch": True}, timeout=90)
    assert code == 200 and direct.get("status") == "emergency_direct"
    assert direct.get("effective_override") == "emergency_direct"
    assert bridge.snapshot_calls() == [], "packet recovery must not call the external provider API"
    _assert_packet_runtime(stack, mode="direct", policy_required=False)
    core_before_emergency = _core_counter_snapshot()
    before_emergency_direct = _packet_counts()
    emergency_phase = _probe_phase(stack, CLIENT_IP, "emergency-direct", core_before_emergency)
    after_emergency_direct = _wait_packet_counts(lambda counts: all(
        _flow_delta(before_emergency_direct, counts, flow) > 0 for flow in (
            (CLIENT_IP, SERVICE_VIP, "tcp", 9080),
            (CLIENT_IP, SERVICE_VIP, "udp", 9081),
            (CLIENT_IP, SERVICE_VIP, "udp", 5353),
        )))
    core_emergency = emergency_phase["core_counters"]
    assert core_emergency["direct_lan_forward"] > 0, {
        "phase": "emergency-direct", "core_counters": core_emergency,
    }
    emergency_flows = {
        "tcp_9080": _flow_delta(before_emergency_direct, after_emergency_direct,
                                (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(before_emergency_direct, after_emergency_direct,
                                (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(before_emergency_direct, after_emergency_direct,
                                (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
    }
    assert all(count > 0 for count in emergency_flows.values()), {
        "phase": "emergency-direct", "flows": emergency_flows,
    }

    _set_health_available(True)
    code, reentered = http_json(f"{api}/__acceptance/provider/reentry", method="POST",
                                payload={"timeout_ms": 2000}, timeout=90)
    assert code == 200 and reentered.get("ok") is True
    assert reentered.get("effective_override") in {None, ""}
    assert bridge.snapshot_calls() == [], "provider reentry must not call the external provider API"
    code, vpn_projection = http_json(f"{api}/routing/global")
    assert code == 200 and vpn_projection.get("ok") is True
    assert vpn_projection["data"]["routing"].get("desired_mode") == "vpn"
    assert vpn_projection["data"]["routing"].get("applied_mode") == "vpn"
    assert vpn_projection["data"]["runtime_enforcement"].get("live_global_mode") == "vpn"
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    core_before_reentry = _core_counter_snapshot()
    before_reentry = _packet_counts()
    reentry_phase = _probe_phase(stack, SERVICE_VIP, "vpn-after-reentry", core_before_reentry)
    after_reentry = _wait_packet_counts(lambda counts:
        _flow_delta(before_reentry, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    core_reentry = reentry_phase["core_counters"]
    assert (core_reentry["vpn_tcp_classified"] > 0 and core_reentry["vpn_udp_classified"] > 0
            and core_reentry["vpn_udp_handoff_packets"] > 0), {
        "phase": "vpn-after-reentry", "core_counters": core_reentry,
    }
    reentry_flow = _flow_delta(before_reentry, after_reentry, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301))
    reentry_direct_leaks = {
        "tcp_9080": _flow_delta(before_reentry, after_reentry, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(before_reentry, after_reentry, (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(before_reentry, after_reentry, (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
    }
    assert reentry_flow > 0 and all(count == 0 for count in reentry_direct_leaks.values()), {
        "phase": "vpn-after-reentry", "vless_endpoint_packets": reentry_flow,
        "direct_client_leaks": reentry_direct_leaks,
    }
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")
    final_binding = binding_for(source_ref)
    assert final_binding["current_member_id"] == binding["current_member_id"]
    assert final_binding["applied_member_id"] == binding["applied_member_id"]
    assert get_vpn_auto_exclusive_source_ref() == source_ref
    code, vpn_state = http_json(f"{api}/state/vpn")
    assert code == 200 and vpn_state.get("ok") is True
    assert vpn_state["data"]["vpn"]["effective"]["server_health"]["active"]["server_id"] == logical_id

    _write_capture_summary({
        "core_counter_mapping": {
            "direct_lan_forward": "inet fwrouter_v2 chain fwrouter_direct / global direct path (exercised by fixed LAN packet phase)",
            "vpn_lan_input": "sum of inet fwrouter_v2 chain fwrouter_vpn_full TCP 5204 REDIR and UDP 5205 TProxy mark counters selected by global vpn v1; Core has no input-hook packet counter",
            "vpn_udp_handoff_packets": "inet fwrouter_v2 chain prerouting / fwrouter full-vpn tproxy handoff udp:5205 counter; proves marked full-VPN UDP reached the Core TProxy handoff",
        },
        "direct_before_vpn": {"flows": direct_flows, "core_counters": core_direct,
                               "mihomo_path": direct_phase["mihomo_path"],
                               "observations": direct_phase["observations"],
                               "egress_guard": direct_phase["leak"],
                               "router_guard": direct_phase["router_guard"]},
        "vpn": {"vless_endpoint_packets": vpn_flow, "direct_client_leaks": vpn_direct_leaks,
                "core_counters": core_vpn,
                "mihomo_path": vpn_phase["mihomo_path"],
                "observations": vpn_phase["observations"],
                "egress_guard": vpn_phase["leak"], "router_guard": vpn_phase["router_guard"]},
        "emergency_direct": {"flows": emergency_flows, "core_counters": core_emergency,
                              "mihomo_path": emergency_phase["mihomo_path"],
                              "observations": emergency_phase["observations"],
                              "egress_guard": emergency_phase["leak"],
                              "router_guard": emergency_phase["router_guard"]},
        "vpn_after_reentry": {"vless_endpoint_packets": reentry_flow,
                               "direct_client_leaks": reentry_direct_leaks,
                               "core_counters": core_reentry,
                               "mihomo_path": reentry_phase["mihomo_path"],
                               "observations": reentry_phase["observations"],
                               "egress_guard": reentry_phase["leak"],
                               "router_guard": reentry_phase["router_guard"]},
    })
