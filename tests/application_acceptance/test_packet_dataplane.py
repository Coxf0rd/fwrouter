from __future__ import annotations

import ipaddress
import ctypes
import hashlib
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
NEGATIVE_CAPTURE_DRAIN_SECONDS = 1.0
CORE_NFT_TABLE = "fwrouter_v2"
ROUTER_GUARD_TABLE = "fwrouter_packet_guard"
MAX_CORE_NFT_JSON = 1024 * 1024
MAX_PHASE_DIAGNOSTIC_BYTES = 4096


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


def _udp_negative_control() -> dict[str, Any]:
    """Return a bounded client-side UDP failure, never interpreting HTTP 200 as packet success."""
    request = urllib.request.Request(f"{CLIENT_CONTROL}/probe/udp-negative", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            code, raw = response.status, response.read(8193)
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read(8193)
    except (OSError, TimeoutError) as exc:
        raise AssertionError("bounded negative UDP control request did not complete") from exc
    assert len(raw) <= 8192, "negative UDP control response exceeded its byte bound"
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("negative UDP control response was not valid bounded JSON") from exc
    assert code == 502 and isinstance(value, dict) and value.get("ok") is False, {
        "control_status": code, "control_body_shape": type(value).__name__,
    }
    assert value.get("error") in {"TimeoutError", "ConnectionRefusedError", "OSError"}, {
        "udp_failure_class": value.get("error"),
    }
    return {"control_status": code, "failure_class": value["error"]}


def _tcp_negative_control() -> dict[str, Any]:
    """Return a bounded client TCP rejection; a successful HTTP response fails the contract."""
    request = urllib.request.Request(f"{CLIENT_CONTROL}/probe/tcp-negative", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            code, raw = response.status, response.read(8193)
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read(8193)
    except (OSError, TimeoutError) as exc:
        raise AssertionError("bounded negative TCP control request did not complete") from exc
    assert len(raw) <= 8192, "negative TCP control response exceeded its byte bound"
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("negative TCP control response was not valid bounded JSON") from exc
    assert code == 502 and isinstance(value, dict) and value.get("ok") is False, {
        "control_status": code, "control_body_shape": type(value).__name__,
    }
    assert value.get("error") in {"TimeoutError", "ConnectionRefusedError", "ConnectionResetError",
                                   "OSError", "FixtureError"}, {
        "tcp_failure_class": value.get("error"),
    }
    return {"control_status": code, "failure_class": value["error"]}


def _set_packet_provider_capability(bridge: Any, capability: str | None) -> None:
    if capability not in {None, "true", "false"}:
        raise AssertionError("packet provider capability is outside the fixed URI matrix")
    declaration = "" if capability is None else f"&udp={capability}"
    row = dict(bridge.current_config)
    row["connection_url"] = (
        f"vless://88c7ce2a-465e-4e72-9c56-2a9e2fc84a51@{ENDPOINT_IP}:5301"
        f"?type=tcp&encryption=none{declaration}#acceptance"
    )
    with bridge._lock:
        bridge.current_config = row


def _refresh_packet_provider(api: str, source_ref: str) -> dict[str, Any]:
    code, accepted = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                               payload={"action": "refresh"})
    assert code == 200 and accepted.get("ok") is True and accepted.get("data", {}).get("accepted") is True
    job = await_core_job(api, accepted)
    assert job.get("status") == "success", {
        "provider_refresh_status": job.get("status"), "provider_refresh_error": job.get("error_code"),
    }
    from fwrouter_api.services.provider_managed import binding_for
    binding = binding_for(source_ref)
    assert isinstance(binding, dict) and binding.get("logical_server_id")
    return binding


def _packet_capability_state(stack: dict[str, Any], source_ref: str, logical_id: str,
                             expected: str, expected_udp: bool,
                             *, expect_active_server: bool = True) -> dict[str, Any]:
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.provider_managed import binding_for

    with db_session() as connection:
        rows = connection.execute(
            "SELECT member_id, member_config_json FROM logical_server_members "
            "WHERE logical_server_id=? AND is_active=1 ORDER BY member_order, member_id",
            (logical_id,),
        ).fetchall()
    assert len(rows) == 1, {"active_member_count": len(rows)}
    member = json.loads(rows[0]["member_config_json"])
    assert isinstance(member, dict) and member.get("_fwrouter_udp_capability") == expected, {
        "member_udp_capability": member.get("_fwrouter_udp_capability") if isinstance(member, dict) else None,
    }
    config_path = stack["state"] / "generated" / "mihomo" / "config.yaml"
    config = yaml.safe_load(config_path.read_bytes())
    proxies = config.get("proxies") if isinstance(config, dict) else None
    vless = [row for row in proxies if isinstance(row, dict) and row.get("type") == "vless"] if isinstance(proxies, list) else []
    assert len(vless) == 1 and vless[0].get("udp") is expected_udp and vless[0].get("port") == 5301, {
        "native_vless_count": len(vless),
        "native_udp": vless[0].get("udp") if len(vless) == 1 else None,
        "native_target_port": vless[0].get("port") if len(vless) == 1 else None,
    }
    assert not any(str(key).startswith("_fwrouter") for key in vless[0]), "internal capability marker leaked into native Mihomo config"
    native = _mihomo_packet_path_proof(stack)
    assert native.get("available") is True and native.get("runtime_running") is True, native
    assert native.get("native_loaded_config_matches_active") is True, native
    assert native.get("vless_udp_enabled_count") == (1 if expected_udp else 0), native
    binding = binding_for(source_ref)
    assert binding["logical_server_id"] == logical_id
    code, vpn_state = http_json(f"{stack['api']}/state/vpn")
    assert code == 200 and vpn_state.get("ok") is True
    active = vpn_state["data"]["vpn"]["effective"]["server_health"]["active"]
    expected_active_id = logical_id if expect_active_server else None
    assert active.get("server_id") == expected_active_id, {
        "active_logical_server_preserved": active.get("server_id"),
        "expected_logical_server": expected_active_id,
    }
    return {"declared_capability": expected, "native_udp_attempt": expected_udp,
            "member_id": rows[0]["member_id"], "native": native}


def _provider_selection_snapshot(source_ref: str) -> dict[str, Any]:
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.provider_managed import binding_for
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    binding = binding_for(source_ref)
    with db_session() as connection:
        fence = read_selection_fence(connection)
    return {
        "logical_server_id": binding.get("logical_server_id"),
        "current_member_id": binding.get("current_member_id"),
        "applied_member_id": binding.get("applied_member_id"),
        "binding_revision": binding.get("binding_revision"),
        "selection_revision": fence.get("revision"),
        "exclusive_source_ref": get_vpn_auto_exclusive_source_ref(),
    }


def _assert_packet_selection_stable(before: dict[str, Any], after: dict[str, Any]) -> None:
    if before != after:
        raise AssertionError({"selection_fence_changed_during_packet_probe": {"before": before, "after": after}})


def _refresh_packet_capability(stack: dict[str, Any], api: str, bridge: Any, source_ref: str, capability: str | None,
                               expected_capability: str, logical_id: str, expected_member: str) -> dict[str, Any]:
    before = _provider_selection_snapshot(source_ref)
    _set_packet_provider_capability(bridge, capability)
    binding = _refresh_packet_provider(api, source_ref)
    after = _provider_selection_snapshot(source_ref)
    assert binding.get("logical_server_id") == logical_id
    assert after["logical_server_id"] == logical_id
    assert after["current_member_id"] == expected_member and after["applied_member_id"] == expected_member, {
        "provider_member_changed_during_capability_refresh": after,
    }
    assert after["exclusive_source_ref"] == source_ref
    assert type(before["selection_revision"]) is int and type(after["selection_revision"]) is int
    assert after["selection_revision"] >= before["selection_revision"], {
        "selection_revision_moved_backwards": {"before": before, "after": after},
    }
    return _packet_capability_state(stack, source_ref, logical_id, expected_capability,
                                    expected_capability != "unsupported")


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
        launch = assert_mihomo_launch_matches_active(stack["native"], config_path)
        native_config_sha256 = launch.get("sha256") if isinstance(launch, dict) else None
        incarnation_sha256 = None
        try:
            incarnation = stack["native"].rpc("mihomo_incarnation", {})
            raw_incarnation = incarnation.get("details", {}).get("stdout") if isinstance(incarnation, dict) else None
            if isinstance(raw_incarnation, str) and raw_incarnation:
                incarnation_sha256 = hashlib.sha256(raw_incarnation.encode("utf-8")).hexdigest()
        except Exception:
            pass
        code, response = http_json(f"{stack['api']}/mihomo")
        data = response.get("data") if isinstance(response, dict) else None
        mihomo = data.get("mihomo") if isinstance(data, dict) else None
        details = mihomo.get("details") if isinstance(mihomo, dict) else None
        selectors = details.get("selectors") if isinstance(details, dict) else None
        selectors = selectors if isinstance(selectors, dict) else {}
        udp_policy = details.get("vpn_udp_policy") if isinstance(details, dict) else None
        udp_policy = udp_policy if isinstance(udp_policy, dict) else {}
        transparent = details.get("transparent_runtime") if isinstance(details, dict) else None
        transparent = transparent if isinstance(transparent, dict) else {}
        count = transparent.get("transparent_udp_sessions_count")
        if type(count) is not int or count < 0 or count > 4096:
            count = None
        current_auto = selectors.get("vpn_auto_now")
        auto_targets = selectors.get("vpn_auto_targets")
        global_targets = selectors.get("vpn_global_targets")
        auto_targets = auto_targets if isinstance(auto_targets, list) else []
        global_targets = global_targets if isinstance(global_targets, list) else []
        return {
            "available": code == 200 and isinstance(mihomo, dict),
            "native_loaded_config_matches_active": True,
            "runtime_running": isinstance(mihomo, dict) and mihomo.get("runtime_state") == "running",
            "vpn_global_selects_vpn_auto": selectors.get("vpn_global_now") == "vpn-auto",
            "vpn_auto_selects_non_direct": isinstance(current_auto, str) and current_auto.upper() != "DIRECT",
            "vpn_auto_selected_reject": str(current_auto or "").upper() == "REJECT",
            "vpn_auto_selected_target_kind": (
                str(selectors.get("vpn_auto_selected_target")).upper()
                if str(selectors.get("vpn_auto_selected_target") or "").upper() in {"REJECT", "DIRECT"}
                else "provider" if selectors.get("vpn_auto_selected_target") else None
            ),
            "vpn_auto_reject_only": auto_targets == ["REJECT"],
            "vpn_auto_has_direct_target": any(str(item).upper() == "DIRECT" for item in auto_targets),
            "vpn_global_has_direct_target": any(str(item).upper() == "DIRECT" for item in global_targets),
            "full_vpn_udp_listener_count": len(full_udp),
            "vless_proxy_count": len(vless),
            "vless_udp_enabled_count": sum(row.get("udp") is True for row in vless),
            "transparent_udp_sessions_count": count,
            "transparent_udp_session_materialized": transparent.get("transparent_udp_session_materialized") is True,
            "active_config_sha256": native_config_sha256 if isinstance(native_config_sha256, str) else None,
            "native_incarnation_sha256": incarnation_sha256,
            "vpn_udp_policy_installed": udp_policy.get("policy_installed") is True,
            "vpn_udp_policy_wiring_proven": udp_policy.get("wiring_proven") is True,
            "vpn_udp_policy_receipt_matches": udp_policy.get("reconcile_receipt_matches") is True,
            "vpn_udp_selector_membership_matches": udp_policy.get("selector_membership_matches") is True,
            "vpn_udp_guarded_rules_match": udp_policy.get("guarded_rules_match") is True,
            "vpn_udp_native_rules_match": udp_policy.get("native_top_level_rules_match") is True,
        }
    except Exception as exc:
        return {"available": False, "error_class": type(exc).__name__[:64]}


def _record_phase_diagnostic(record_property: Any, diagnostic: dict[str, Any]) -> None:
    """Persist only the fixed, bounded packet-path observations in JUnit."""
    label = diagnostic.get("phase")
    if label not in {"direct-before-vpn", "vpn", "vpn-supported-udp", "vpn-unknown-udp",
                     "vpn-unsupported-udp", "vpn-endpoint-unavailable", "vpn-empty-pool",
                     "vpn-tcp-after-unknown", "vpn-tcp-after-unsupported", "vpn-empty-pool-tcp",
                     "emergency-direct", "vpn-after-reentry", "provider-enable-job",
                     "vpn-auto-exclusive-job", "global-mode-direct", "global-mode-vpn"}:
        raise AssertionError("packet phase diagnostic has an unknown fixed phase")
    encoded = json.dumps(diagnostic, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("ascii")) > MAX_PHASE_DIAGNOSTIC_BYTES:
        raise AssertionError("packet phase diagnostic exceeded its fixed byte bound")
    record_property("packet_phase_" + label.replace("-", "_"), encoded)


def _bounded_job_summary(job: Any) -> dict[str, Any]:
    """Keep failure attribution useful while excluding payloads, IDs, and messages."""
    if not isinstance(job, dict):
        return {"shape": type(job).__name__[:48]}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    apply = result.get("apply") if isinstance(result.get("apply"), dict) else {}
    verification = apply.get("verification") if isinstance(apply.get("verification"), dict) else {}
    target_readback = result.get("target_readback")
    if not isinstance(target_readback, dict):
        target_readback = verification.get("target_readback")
    target_readback = target_readback if isinstance(target_readback, dict) else {}
    def short(value: Any) -> str | None:
        return value[:96] if isinstance(value, str) and value and all(ch.isalnum() or ch in "_.:-" for ch in value) else None
    def boolean(value: Any) -> bool | None:
        return value if type(value) is bool else None
    return {
        "status": short(job.get("status")),
        "job_type": short(job.get("job_type")),
        "error_code": short(job.get("error_code") or result.get("error_code")),
        "result_status": short(result.get("job_status")),
        "stage": short(result.get("stage")),
        "runtime_verified": boolean(result.get("runtime_verified")),
        "intent_saved": boolean(result.get("intent_saved")),
        "last_good_retained": boolean(result.get("last_good_retained")),
        "apply_reconcile_action": short(apply.get("reconcile_action")),
        "apply_reconcile_reason": short(apply.get("reconcile_reason")),
        "apply_verification_error": short(verification.get("error_code")),
        "target_readback_matches": boolean(target_readback.get("target_set_matches")),
        "target_expected_count": target_readback.get("expected_target_count")
        if type(target_readback.get("expected_target_count")) is int else None,
        "target_observed_count": target_readback.get("observed_target_count")
        if type(target_readback.get("observed_target_count")) is int else None,
        "mihomo_reconcile": _bounded_reconcile_result(result),
    }


def _bounded_reconcile_result(result: dict[str, Any]) -> dict[str, Any]:
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    reconcile = details.get("mihomo_reconcile") if isinstance(details.get("mihomo_reconcile"), dict) else {}
    native = reconcile.get("native_udp_policy_proof")
    native = native if isinstance(native, dict) else {}
    rollback = reconcile.get("rollback")
    rollback = rollback if isinstance(rollback, dict) else {}
    safe_native_keys = (
        "ok", "native_top_level_rules_match", "selector_membership_matches",
        "guarded_rules_match", "wiring_proven",
    )
    def short(value: Any) -> str | None:
        return value[:96] if isinstance(value, str) and value and all(ch.isalnum() or ch in "_.:-" for ch in value) else None
    def boolean(value: Any) -> bool | None:
        return value if type(value) is bool else None
    return {
        "ok": boolean(reconcile.get("ok")),
        "stage": short(reconcile.get("stage")),
        "error_code": short(reconcile.get("error_code")),
        "reconcile_reason": short(reconcile.get("reconcile_reason") or reconcile.get("reason")),
        "reconcile_action": short(reconcile.get("reconcile_action")),
        "native_udp_policy_proof": {key: boolean(native.get(key)) for key in safe_native_keys
                                     if type(native.get(key)) is bool},
        "rollback_ok": boolean(rollback.get("ok")),
        "rollback_recovered": boolean(rollback.get("recovered")),
        "rollback_reason": short(rollback.get("reason")),
    }


def _record_job_stage(record_property: Any, phase: str, *, http_status: int,
                      accepted: Any, job: Any = None, error_class: str | None = None,
                      stack: dict[str, Any]) -> None:
    revision = None
    try:
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        from fwrouter_api.db.connection import db_session

        with db_session() as connection:
            fence = read_selection_fence(connection)
        revision = fence.get("revision") if isinstance(fence, dict) else None
    except Exception:
        pass
    diagnostic = {
        "phase": phase,
        "http_status": http_status if type(http_status) is int else None,
        "accepted": (accepted.get("data", {}).get("accepted") is True
                     if isinstance(accepted, dict) and isinstance(accepted.get("data"), dict) else False),
        "job": _bounded_job_summary(job),
        "await_error_class": error_class[:64] if isinstance(error_class, str) else None,
        "selection_revision": revision if type(revision) is int else None,
        "mihomo_path": _mihomo_packet_path_proof(stack),
    }
    _record_phase_diagnostic(record_property, diagnostic)


def _bounded_reconcile_summary(response: Any) -> dict[str, Any]:
    """Extract only allowlisted reconciliation state from a global-mode API error."""
    if not isinstance(response, dict):
        return {"shape": type(response).__name__[:48]}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    mutation = data.get("routing_mutation") if isinstance(data.get("routing_mutation"), dict) else {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    job_result = job.get("result") if isinstance(job.get("result"), dict) else {}
    job_details = job_result.get("details") if isinstance(job_result.get("details"), dict) else {}
    reconcile = next((value.get("mihomo_reconcile") for value in
                      (details, data, mutation, job_result, job_details)
                      if isinstance(value.get("mihomo_reconcile"), dict)), {})
    native = reconcile.get("native_udp_policy_proof")
    native = native if isinstance(native, dict) else {}
    rollback = reconcile.get("rollback")
    rollback = rollback if isinstance(rollback, dict) else {}
    def short(value: Any) -> str | None:
        return value[:96] if isinstance(value, str) and value and all(ch.isalnum() or ch in "_.:-" for ch in value) else None
    def boolean(value: Any) -> bool | None:
        return value if type(value) is bool else None
    safe_native_keys = (
        "ok", "native_top_level_rules_match", "selector_membership_matches",
        "guarded_rules_match", "wiring_proven",
    )
    safe_fence_keys = (
        "selection_fenced", "selection_revision_matches", "runtime_incarnation_matches",
        "receipt_matches", "publication_still_owned",
    )
    fence = reconcile.get("fence") if isinstance(reconcile.get("fence"), dict) else {}
    return {
        "api_error_code": short(error.get("code") or data.get("code")),
        "api_error_stage": short(error.get("stage") or data.get("stage")),
        "reconcile_ok": boolean(reconcile.get("ok")),
        "reconcile_stage": short(reconcile.get("stage")),
        "reconcile_error_code": short(reconcile.get("error_code")),
        "reconcile_reason": short(reconcile.get("reconcile_reason") or reconcile.get("reason")),
        "reconcile_action": short(reconcile.get("reconcile_action")),
        "native_udp_policy_proof": {key: boolean(native.get(key)) for key in safe_native_keys
                                     if type(native.get(key)) is bool},
        "fence_proof": {key: boolean(fence.get(key)) for key in safe_fence_keys
                         if type(fence.get(key)) is bool},
        "rollback_ok": boolean(rollback.get("ok")),
        "rollback_recovered": boolean(rollback.get("recovered")),
        "rollback_reason": short(rollback.get("reason")),
        "rollback_error_code": short(rollback.get("error_code")),
    }


def _probe_phase(stack: dict[str, Any], expected_peer: str, label: str,
                 core_before: dict[str, int], record_property: Any, source_ref: str) -> dict[str, Any]:
    selection_before = _provider_selection_snapshot(source_ref)
    before = _endpoint_observations()
    tcp = _role_json(f"{CLIENT_CONTROL}/probe/tcp", method="POST")
    udp = _role_json(f"{CLIENT_CONTROL}/probe/udp", method="POST")
    dns = _role_json(f"{CLIENT_CONTROL}/probe/dns", method="POST")
    negative = _role_json(f"{CLIENT_CONTROL}/probe/dns-negative", method="POST")
    leak = _role_json(f"{CLIENT_CONTROL}/probe/leak", method="POST")
    after = _endpoint_observations()
    selection_after = _provider_selection_snapshot(source_ref)
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
        "provider_selection_before": selection_before,
        "provider_selection_after": selection_after,
    }
    _record_phase_diagnostic(record_property, diagnostic)
    _assert_packet_selection_stable(selection_before, selection_after)
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


def _wait_direct_udp_capture_quiet(baseline: dict[tuple[str, str, str, int], int],
                                   *, timeout_seconds: float = NEGATIVE_CAPTURE_DRAIN_SECONDS) -> dict[tuple[str, str, str, int], int]:
    """Drain owned capture writes for the failed probe without a sleep or an unbounded wait."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise AssertionError("bounded negative capture observer could not initialize")
    selector = selectors.DefaultSelector()
    try:
        if libc.inotify_add_watch(fd, os.fsencode(UDP_CAPTURE_PATH), 0x00000002) < 0:
            raise AssertionError("bounded negative capture observer could not watch its owned UDP capture")
        selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = _packet_counts()
            direct = sum(_flow_delta(baseline, current, flow) for flow in (
                (CLIENT_IP, SERVICE_VIP, "udp", 9081),
                (ROUTER_WAN_IP, SERVICE_VIP, "udp", 9081),
            ))
            if direct > 0:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return _packet_counts()
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
                        raise AssertionError("negative capture observer queue overflowed")
                    offset += 16 + name_length
    finally:
        selector.close()
        os.close(fd)


def _wait_direct_tcp_capture_quiet(baseline: dict[tuple[str, str, str, int], int],
                                   *, timeout_seconds: float = NEGATIVE_CAPTURE_DRAIN_SECONDS) -> dict[tuple[str, str, str, int], int]:
    """Boundedly observe TCP capture writes while rejecting direct VIP connections."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise AssertionError("bounded negative TCP capture observer could not initialize")
    selector = selectors.DefaultSelector()
    try:
        if libc.inotify_add_watch(fd, os.fsencode(TCP_CAPTURE_PATH), 0x00000002) < 0:
            raise AssertionError("bounded negative TCP capture observer could not watch its owned capture")
        selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = _packet_counts()
            direct = sum(_flow_delta(baseline, current, flow) for flow in (
                (CLIENT_IP, SERVICE_VIP, "tcp", 9080),
                (ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080),
            ))
            if direct > 0:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return _packet_counts()
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
                        raise AssertionError("negative TCP capture observer queue overflowed")
                    offset += 16 + name_length
    finally:
        selector.close()
        os.close(fd)


def _probe_vpn_udp_contract_phase(stack: dict[str, Any], source_ref: str, logical_id: str,
                                  capability: str, expected_udp: bool, label: str,
                                  record_property: Any, *, expect_success: bool,
                                  endpoint_unavailable: bool = False,
                                  expect_vpn_handoff: bool | None = None,
                                  expect_active_server: bool = True) -> dict[str, Any]:
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    capability_state = _packet_capability_state(stack, source_ref, logical_id, capability, expected_udp,
                                                 expect_active_server=expect_active_server)
    selection_before = _provider_selection_snapshot(source_ref)
    observations_before = _endpoint_observations()
    capture_before = _packet_counts()
    core_before = _core_counter_snapshot()
    if endpoint_unavailable:
        endpoint_state = _role_json(f"{ENDPOINT_CONTROL}/ready")
        assert endpoint_state.get("sockets", {}).get("xray_tcp_5301") is False, endpoint_state
        attempt = _udp_negative_control()
        capture_after = _wait_direct_udp_capture_quiet(capture_before)
    elif expect_success:
        attempt = _role_json(f"{CLIENT_CONTROL}/probe/udp", method="POST")
        capture_after = _wait_packet_counts(lambda counts:
            _flow_delta(capture_before, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    else:
        attempt = _udp_negative_control()
        capture_after = _wait_direct_udp_capture_quiet(capture_before)
    observations_after = _endpoint_observations()
    selection_after = _provider_selection_snapshot(source_ref)
    core_delta = _core_counter_delta(core_before, _core_counter_snapshot())
    direct_flow_deltas = {
        "client_source_to_service": _flow_delta(capture_before, capture_after,
                                                 (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "mihomo_direct_to_service": _flow_delta(capture_before, capture_after,
                                                 (ROUTER_WAN_IP, SERVICE_VIP, "udp", 9081)),
    }
    endpoint_udp_delta = observations_after["counts"].get("udp", -1) - observations_before["counts"].get("udp", 0)
    diagnostic = {
        "phase": label,
        "capability": capability_state,
        "attempt": attempt,
        "expected_success": expect_success,
        "endpoint_unavailable": endpoint_unavailable,
        "endpoint_udp_observation_delta": endpoint_udp_delta,
        "core_counter_delta": core_delta,
        "direct_udp_capture_delta": direct_flow_deltas,
        "provider_selection_before": selection_before,
        "provider_selection_after": selection_after,
        "endpoint_xray_running": endpoint_state.get("xray", {}).get("running") if endpoint_unavailable else True,
    }
    _record_phase_diagnostic(record_property, diagnostic)
    _assert_packet_selection_stable(selection_before, selection_after)
    if expect_vpn_handoff is None:
        # Unsupported and empty-pool packets still enter Mihomo through TPROXY;
        # its generated UDP REJECT policy must stop them there without DIRECT.
        expect_vpn_handoff = True
    assert core_delta["vpn_udp_classified"] > 0, diagnostic
    assert (core_delta["vpn_udp_handoff_packets"] > 0) is expect_vpn_handoff, diagnostic
    # This counter is the shared fwrouter_direct terminal-chain total; host-output
    # and host-local control traffic also reach it. Leak proof is tuple-scoped below.
    assert direct_flow_deltas == {"client_source_to_service": 0, "mihomo_direct_to_service": 0}, diagnostic
    if expect_success:
        assert attempt.get("ok") is True and attempt.get("peer") == SERVICE_VIP, diagnostic
        assert endpoint_udp_delta == 1 and observations_after["last_peers"].get("udp") == SERVICE_VIP, diagnostic
        assert _flow_delta(capture_before, capture_after,
                           (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0, diagnostic
    else:
        assert attempt.get("control_status") == 502 and attempt.get("failure_class") in {
            "TimeoutError", "ConnectionRefusedError", "OSError",
        }, diagnostic
        assert endpoint_udp_delta == 0, diagnostic
        assert observations_after["counts"].get("udp", -1) == observations_before["counts"].get("udp", 0), diagnostic
    return diagnostic


def _probe_vpn_tcp_regression(stack: dict[str, Any], label: str,
                              record_property: Any) -> dict[str, Any]:
    """Prove TCP still traverses the selected VPN proxy after UDP capability changes."""
    before = _packet_counts()
    core_before = _core_counter_snapshot()
    endpoint_before = _endpoint_observations()
    result = _role_json(f"{CLIENT_CONTROL}/probe/tcp", method="POST")
    after = _wait_packet_counts(lambda counts:
        _flow_delta(before, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    core_delta = _core_counter_delta(core_before, _core_counter_snapshot())
    endpoint_after = _endpoint_observations()
    direct = {
        "client": _flow_delta(before, after, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "mihomo": _flow_delta(before, after, (ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080)),
    }
    diagnostic = {
        "phase": label,
        "result": result,
        "core_counter_delta": core_delta,
        "endpoint_http_delta": endpoint_after["counts"].get("http", -1) - endpoint_before["counts"].get("http", 0),
        "vpn_proxy_capture_delta": _flow_delta(before, after, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)),
        "direct_tcp_capture_delta": direct,
        "mihomo_path": _mihomo_packet_path_proof(stack),
    }
    _record_phase_diagnostic(record_property, diagnostic)
    assert result.get("ok") is True and result.get("peer") == SERVICE_VIP, diagnostic
    assert core_delta["vpn_tcp_classified"] > 0, diagnostic
    assert diagnostic["vpn_proxy_capture_delta"] > 0 and diagnostic["endpoint_http_delta"] > 0, diagnostic
    assert direct == {"client": 0, "mihomo": 0}, diagnostic
    return diagnostic


def _probe_vpn_tcp_empty_pool(stack: dict[str, Any], record_property: Any) -> dict[str, Any]:
    """Full-VPN TCP remains classified but has no target or DIRECT escape."""
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    before = _packet_counts()
    core_before = _core_counter_snapshot()
    endpoint_before = _endpoint_observations()
    attempt = _tcp_negative_control()
    after = _wait_direct_tcp_capture_quiet(before)
    core_delta = _core_counter_delta(core_before, _core_counter_snapshot())
    endpoint_after = _endpoint_observations()
    deltas = {
        "client_direct": _flow_delta(before, after, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "router_direct": _flow_delta(before, after, (ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080)),
        "vpn_proxy": _flow_delta(before, after, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)),
    }
    endpoint_http_delta = endpoint_after["counts"].get("http", -1) - endpoint_before["counts"].get("http", 0)
    diagnostic = {"phase": "vpn-empty-pool-tcp", "attempt": attempt,
                  "core_counter_delta": core_delta, "capture_delta": deltas,
                  "endpoint_http_observation_delta": endpoint_http_delta}
    _record_phase_diagnostic(record_property, diagnostic)
    assert core_delta["vpn_tcp_classified"] > 0, diagnostic
    # The shared fwrouter_direct counter includes host-output control traffic;
    # exact client/router destination tuples below are the direct-leak assertion.
    assert deltas == {"client_direct": 0, "router_direct": 0, "vpn_proxy": 0}, diagnostic
    assert endpoint_http_delta == 0, diagnostic
    return diagnostic


def _set_health_available(available: bool) -> None:
    result = _role_json(f"{ENDPOINT_CONTROL}/health", method="POST", payload={"available": available})
    assert result.get("available") is available, {"health_control": "unexpected response"}


def _apply_global_mode(api: str, mode: str, *, stack: dict[str, Any], record_property: Any) -> dict[str, Any]:
    phase = f"global-mode-{mode}"
    code, accepted = http_json(f"{api}/routing/global", method="POST",
                               payload={"mode": mode, "requested_by": "hosted-packet-acceptance", "run_now": True})
    job = None
    await_error_class = None
    if code == 200 and isinstance(accepted, dict) and accepted.get("ok") is True:
        try:
            job = await_core_job(api, accepted)
        except Exception as exc:
            await_error_class = type(exc).__name__
    _record_phase_diagnostic(record_property, {
        "phase": phase,
        "http_status": code if type(code) is int else None,
        "accepted": isinstance(accepted, dict) and accepted.get("ok") is True,
        "api_error": _bounded_reconcile_summary(accepted),
        "job": _bounded_job_summary(job),
        "await_error_class": await_error_class[:64] if isinstance(await_error_class, str) else None,
        "mihomo_path": _mihomo_packet_path_proof(stack),
    })
    assert code == 200 and accepted.get("ok") is True, {"mode": mode, "accepted": accepted.get("ok")}
    if await_error_class is not None:
        raise AssertionError("global mode job polling failed within its bound")
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


def _set_server_auto_eligibility(api: str, logical_id: str, enabled: bool) -> dict[str, Any]:
    code, response = http_json(
        f"{api}/servers/{logical_id}/preferences", method="PATCH",
        payload={"vpn_auto": enabled, "reconcile_mihomo": True,
                 "requested_by": "hosted-packet-acceptance-empty-pool"},
    )
    data = response.get("data") if isinstance(response, dict) else None
    result = data.get("server_preferences") if isinstance(data, dict) else None
    assert code == 200 and response.get("ok") is True and isinstance(result, dict), {
        "auto_eligibility_status": code,
        "auto_eligibility_error": response.get("error") if isinstance(response, dict) else "invalid_response",
    }
    preferences = (result.get("server") or {}).get("preferences") or {}
    assert preferences.get("vpn_auto") is enabled, {
        "expected_vpn_auto": enabled, "observed_vpn_auto": preferences.get("vpn_auto"),
    }
    reconcile = result.get("mihomo_reconcile")
    assert not isinstance(reconcile, dict) or reconcile.get("ok") is True, {
        "mihomo_reconcile": reconcile,
    }
    return result


def _server_user_preferences(api: str, logical_id: str) -> dict[str, Any]:
    code, response = http_json(f"{api}/servers/{logical_id}")
    server = response.get("data", {}).get("server") if isinstance(response, dict) else None
    preferences = server.get("preferences") if isinstance(server, dict) else None
    assert code == 200 and response.get("ok") is True and isinstance(preferences, dict), {
        "server_preferences_readback": code,
    }
    keys = ("vpn_auto", "vpn_auto_priority", "vpn_auto_priority_origin", "global_list",
            "remembered_until", "manually_deleted_at")
    return {key: preferences.get(key) for key in keys}


def _assert_native_empty_vpn_pool(stack: dict[str, Any]) -> dict[str, Any]:
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    config_path = stack["state"] / "generated" / "mihomo" / "config.yaml"
    config = yaml.safe_load(config_path.read_bytes())
    groups = config.get("proxy-groups") if isinstance(config, dict) else None
    auto = next((row for row in groups if isinstance(row, dict) and row.get("name") == "vpn-auto"), None) \
        if isinstance(groups, list) else None
    global_group = next((row for row in groups if isinstance(row, dict) and row.get("name") == "vpn-global"), None) \
        if isinstance(groups, list) else None
    assert isinstance(auto, dict) and auto.get("proxies") == ["REJECT"], {
        "vpn_auto_group": auto,
    }
    assert isinstance(global_group, dict) and "DIRECT" not in global_group.get("proxies", []), {
        "vpn_global_group": global_group,
    }
    code, response = http_json(f"{stack['api']}/mihomo")
    assert code == 200 and response.get("ok") is True
    details = response["data"]["mihomo"].get("details") or {}
    selectors = details.get("selectors") or {}
    assert (selectors.get("vpn_auto_now") is None
            and selectors.get("vpn_auto_selected_target") == "REJECT"
            and selectors.get("vpn_auto_targets") == ["REJECT"]), {
        "native_vpn_auto_selection": selectors.get("vpn_auto_now"),
        "native_vpn_auto_selected_target": selectors.get("vpn_auto_selected_target"),
        "native_vpn_auto_targets": selectors.get("vpn_auto_targets"),
    }
    code, vpn_state = http_json(f"{stack['api']}/state/vpn")
    active = vpn_state.get("data", {}).get("vpn", {}).get("effective", {}).get("server_health", {}).get("active") \
        if isinstance(vpn_state, dict) else None
    assert code == 200 and vpn_state.get("ok") is True and isinstance(active, dict) \
        and active.get("server_id") is None, {"empty_pool_active_server": active}
    code, routing = http_json(f"{stack['api']}/routing/global")
    enforcement = routing.get("data", {}).get("runtime_enforcement") if isinstance(routing, dict) else None
    assert code == 200 and routing.get("ok") is True and isinstance(enforcement, dict), {
        "global_vpn_enforcement_readback": code,
    }
    assert (enforcement.get("active_mode_matches_intent") is True
            and enforcement.get("enforcement_level") == "global_vpn_fail_closed"
            and enforcement.get("vpn_egress_ready") is False), enforcement
    assert_mihomo_launch_matches_active(stack["native"], config_path)
    return {"vpn_auto_selected_reject": True, "vpn_auto_target_count": 1,
            "vpn_global_has_direct_target": "DIRECT" in global_group["proxies"],
            "runtime_enforcement": {
                "level": enforcement["enforcement_level"],
                "vpn_egress_ready": enforcement["vpn_egress_ready"],
            }}


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
def test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks(
        acceptance_stack, record_property):
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
    _set_packet_provider_capability(bridge, "true")
    _configure_provider(api, source_ref)
    code, enabled = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                              payload={"action": "enable"})
    enable_job = None
    enable_error_class = None
    if code == 200 and enabled.get("ok") is True and enabled.get("data", {}).get("accepted") is True:
        try:
            enable_job = await_core_job(api, enabled)
        except Exception as exc:
            enable_error_class = type(exc).__name__
    _record_job_stage(record_property, "provider-enable-job", http_status=code,
                      accepted=enabled, job=enable_job, error_class=enable_error_class, stack=stack)
    assert code == 200 and enabled.get("ok") is True and enabled.get("data", {}).get("accepted") is True
    if enable_error_class is not None:
        raise AssertionError("provider enable job polling failed within its bound")
    assert enable_job.get("status") == "success"
    from fwrouter_api.services.provider_managed import binding_for
    binding = binding_for(source_ref)
    logical_id = binding["logical_server_id"]
    original_member_id = binding["current_member_id"]
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    code, exclusive = http_json(f"{api}/subscription/sources/{source_ref}/vpn-auto-exclusive", method="POST",
                                payload={"enabled": True})
    exclusive_job = None
    exclusive_error_class = None
    if code == 200 and exclusive.get("ok") is True and exclusive.get("data", {}).get("accepted") is True:
        try:
            exclusive_job = await_core_job(api, exclusive)
        except Exception as exc:
            exclusive_error_class = type(exc).__name__
    _record_job_stage(record_property, "vpn-auto-exclusive-job", http_status=code,
                      accepted=exclusive, job=exclusive_job, error_class=exclusive_error_class, stack=stack)
    assert code == 200 and exclusive.get("ok") is True and exclusive.get("data", {}).get("accepted") is True
    if exclusive_error_class is not None:
        raise AssertionError("exclusive provider job polling failed within its bound")
    assert exclusive_job.get("status") == "success"
    assert get_vpn_auto_exclusive_source_ref() == source_ref

    _set_health_available(True)
    _apply_global_mode(api, "direct", stack=stack, record_property=record_property)
    _assert_packet_runtime(stack, mode="direct", policy_required=False)
    capture_baseline = _packet_counts()
    core_before_direct = _core_counter_snapshot()
    direct_phase = _probe_phase(stack, CLIENT_IP, "direct-before-vpn", core_before_direct, record_property,
                                source_ref)
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

    _apply_global_mode(api, "vpn", stack=stack, record_property=record_property)
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    supported_capability = _packet_capability_state(stack, source_ref, logical_id, "supported", True)
    before_vpn = _packet_counts()
    core_before_vpn = _core_counter_snapshot()
    vpn_phase = _probe_phase(stack, SERVICE_VIP, "vpn-supported-udp", core_before_vpn, record_property,
                             source_ref)
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

    unknown_capability = _refresh_packet_capability(
        stack, api, bridge, source_ref, None, "unknown", logical_id, original_member_id,
    )
    unknown_udp_phase = _probe_vpn_udp_contract_phase(
        stack, source_ref, logical_id, "unknown", True, "vpn-unknown-udp", record_property,
        expect_success=True,
    )
    unknown_tcp_phase = _probe_vpn_tcp_regression(stack, "vpn-tcp-after-unknown", record_property)
    unsupported_capability = _refresh_packet_capability(
        stack, api, bridge, source_ref, "false", "unsupported", logical_id, original_member_id,
    )
    unsupported_udp_phase = _probe_vpn_udp_contract_phase(
        stack, source_ref, logical_id, "unsupported", False, "vpn-unsupported-udp", record_property,
        expect_success=False,
    )
    unsupported_tcp_phase = _probe_vpn_tcp_regression(stack, "vpn-tcp-after-unsupported", record_property)
    supported_again = _refresh_packet_capability(
        stack, api, bridge, source_ref, "true", "supported", logical_id, original_member_id,
    )

    stopped = _role_json(f"{ENDPOINT_CONTROL}/xray/availability", method="POST",
                         payload={"available": False})
    assert stopped.get("available") is False, {"endpoint_xray_stop": stopped}
    unavailable_udp_phase = _probe_vpn_udp_contract_phase(
        stack, source_ref, logical_id, "supported", True, "vpn-endpoint-unavailable", record_property,
        expect_success=False, endpoint_unavailable=True,
    )
    restarted = _role_json(f"{ENDPOINT_CONTROL}/xray/availability", method="POST",
                           payload={"available": True})
    assert restarted.get("available") is True, {"endpoint_xray_restart": restarted}
    endpoint_restored = _role_json(f"{ENDPOINT_CONTROL}/ready")
    assert endpoint_restored.get("ready") is True and endpoint_restored.get("xray", {}).get("running") is True

    # Empty the eligible Auto pool through the public preference API while full-VPN
    # is applied. The native selector must select REJECT, and real UDP must fail
    # without reaching either direct endpoint path.
    preferences_before_empty_pool = _server_user_preferences(api, logical_id)
    assert preferences_before_empty_pool.get("vpn_auto") is True, {
        "empty_pool_requires_selected_provider_to_be_eligible": preferences_before_empty_pool,
    }
    _set_server_auto_eligibility(api, logical_id, False)
    empty_pool_native = _assert_native_empty_vpn_pool(stack)
    empty_pool_phase = _probe_vpn_udp_contract_phase(
        stack, source_ref, logical_id, "supported", True, "vpn-empty-pool", record_property,
        expect_success=False, expect_active_server=False,
    )
    empty_pool_tcp_phase = _probe_vpn_tcp_empty_pool(stack, record_property)
    _set_server_auto_eligibility(api, logical_id, True)
    preferences_after_empty_pool = _server_user_preferences(api, logical_id)
    assert preferences_after_empty_pool == preferences_before_empty_pool, {
        "provider_user_preferences_not_restored_exactly": {
            "before": preferences_before_empty_pool,
            "after": preferences_after_empty_pool,
        },
    }
    restored_pool_native = _assert_packet_runtime(stack, mode="vpn", policy_required=True)

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
    emergency_phase = _probe_phase(stack, CLIENT_IP, "emergency-direct", core_before_emergency,
                                   record_property, source_ref)
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
    reentry_phase = _probe_phase(stack, SERVICE_VIP, "vpn-after-reentry", core_before_reentry,
                                 record_property, source_ref)
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
            "direct_lan_forward": "unscoped inet fwrouter_v2 fwrouter_direct / global direct path counter; also counts host-output and host-local direct branches, diagnostic only (not flow-specific leak proof)",
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
        "udp_capability_contract": {
            "supported": supported_capability["declared_capability"] == "supported",
            "supported_packet": {
                "endpoint_udp_observation": vpn_phase["observations"]["counts"].get("udp", 0),
                "core": core_vpn,
                "vless_tunnel_tcp_capture_packets": vpn_flow,
                "direct_udp_packets": vpn_direct_leaks["udp_9081"],
            },
            "unknown": unknown_capability["declared_capability"] == "unknown",
            "unknown_packet": {"endpoint_udp_delta": unknown_udp_phase["endpoint_udp_observation_delta"],
                               "core": unknown_udp_phase["core_counter_delta"],
                               "tcp": unknown_tcp_phase["vpn_proxy_capture_delta"]},
            "unsupported": unsupported_capability["declared_capability"] == "unsupported",
            "unsupported_packet": {"endpoint_udp_delta": unsupported_udp_phase["endpoint_udp_observation_delta"],
                                   "core": unsupported_udp_phase["core_counter_delta"],
                                   "tcp": unsupported_tcp_phase["vpn_proxy_capture_delta"]},
            "endpoint_unavailable": {"endpoint_udp_delta": unavailable_udp_phase["endpoint_udp_observation_delta"],
                                      "core": unavailable_udp_phase["core_counter_delta"]},
            "empty_pool": {"native": empty_pool_native,
                           "endpoint_udp_delta": empty_pool_phase["endpoint_udp_observation_delta"],
                           "core": empty_pool_phase["core_counter_delta"],
                           "tcp": empty_pool_tcp_phase["core_counter_delta"],
                           "tcp_capture": empty_pool_tcp_phase["capture_delta"]},
        },
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
