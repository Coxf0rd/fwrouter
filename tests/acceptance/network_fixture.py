#!/usr/bin/env python3
"""Bounded client and endpoint roles for the hosted packet dataplane fixture.

The launcher owns namespace creation, fixed addressing, and the read-only profile
mount.  This module accepts no network destinations or commands from callers.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import selectors
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


PROFILE_PATH = Path("/run/fwrouter-acceptance/profile.json")
PROFILE_SCHEMA = "fwrouter-acceptance-profile/v2"
PROFILE_NAME = "hosted-kernel-packet"
PROFILE_BLOCK = "network_testbed"
OWNED_ROOT = Path("/tmp/fwrouter-packet-fixture")
OWNED_MARKER = ".fwrouter-packet-fixture-owned"
OWNED_MARKER_CONTENT = b"FWROUTER_PACKET_FIXTURE_ROOT_V1\n"
DOCKER_MARKER = Path("/.dockerenv")
SOURCE_ROOT = Path("/workspace")
FIXED_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
PRODUCTION_MARKERS = (
    Path("/opt/fwrouter-api"), Path("/opt/fwrouter-xray"),
    Path("/opt/fwrouter-mihomo"), Path("/var/lib/fwrouter-v2"),
    Path("/run/fwrouter-v2"), Path("/etc/systemd/system/fwrouter-api.service"),
)

CLIENT_NET = "10.240.0.0/29"
CLIENT_IP = "10.240.0.2"
ROUTER_LAN_IP = "10.240.0.1"
ROUTER_WAN_IP = "198.18.240.1"
ENDPOINT_WAN_IP = "198.18.240.2"
WAN_NET = "198.18.240.0/29"
SERVICE_VIP = "203.0.113.53"
ENDPOINT_XRAY_PORT = 5301
HTTP_PORT = 9080
UDP_ECHO_PORT = 9081
DNS_PORT = 5353
CLIENT_CONTROL_PORT = 8081
ENDPOINT_CONTROL_PORT = 8082
XRAY_PATH = Path("/opt/fwrouter-test/bin/xray")
XRAY_ID = "88c7ce2a-465e-4e72-9c56-2a9e2fc84a51"
DNS_NAME = "probe.fwrouter.test"
DNS_NEGATIVE_NAME = "missing.fwrouter.test"
DNS_IP = SERVICE_VIP
PROBE_BODY = b"fwrouter-packet-fixture-v1"
LEAK_DNS_TARGET = "127.0.0.11"
LEAK_RESERVED_TARGET = "192.0.2.1"
LEAK_RESERVED_PORT = 9
CONTROL_TIMEOUT = 3.0
READINESS_TIMEOUT = 8.0
MAX_OUTPUT = 8192
MAX_HTTP_BODY = 1024
MAX_JSON_RESPONSE = 8192
NET_ADMIN_BIT = 1 << 12
TABLE_NAME = "fwrouter_packet_fixture"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


class FixtureError(RuntimeError):
    """The packet fixture failed a confinement or protocol check."""


def _redact(value: bytes | str, limit: int = MAX_OUTPUT) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    text = re.sub(r"(?i)(password|secret|token|authorization|credential)(\s*[:=]\s*)[^\s,;]+",
                  r"\1\2[REDACTED]", text)
    text = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b",
                  "[UUID]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    raw = text.encode("utf-8", "replace")
    if len(raw) > limit:
        raw = raw[:limit]
    return raw.decode("utf-8", "replace")


def _read_json(path: Path, *, max_bytes: int = 128 * 1024,
               require_root_owned: bool = True) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise FixtureError("packet fixture profile/config is unavailable") from exc
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222
            or require_root_owned and (info.st_uid != 0 or info.st_gid != 0)):
        raise FixtureError("packet fixture profile/config must be regular and read-only with expected ownership")
    if info.st_size > max_bytes:
        raise FixtureError("packet fixture profile/config exceeds its byte limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FixtureError("packet fixture profile/config is not valid bounded JSON") from exc
    if not isinstance(value, dict):
        raise FixtureError("packet fixture profile/config must be a JSON object")
    return value


def _mount_is_read_only(target: Path) -> bool:
    """Require the exact profile/config mount to be read-only in this namespace."""
    try:
        rows = Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return False
    wanted = str(target)
    for row in rows:
        fields = row.split()
        if len(fields) > 6 and fields[4].replace("\\040", " ") == wanted:
            return "ro" in fields[5].split(",")
    return False


def _tmpfs_is_bounded() -> bool:
    try:
        rows = Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return False
    for row in rows:
        fields = row.split()
        if len(fields) < 10 or fields[4].replace("\\040", " ") != "/tmp" or "-" not in fields:
            continue
        marker = fields.index("-")
        if marker + 3 >= len(fields) or fields[marker + 1] != "tmpfs":
            return False
        options = set(fields[marker + 3].split(","))
        size_value = next((item[5:] for item in options if item.startswith("size=")), "")
        match = re.fullmatch(r"([0-9]+)([kKmMgG]?)", size_value)
        if not match:
            return False
        amount = int(match.group(1))
        multiplier = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match.group(2).lower()]
        return 0 < amount * multiplier <= 1024 ** 3
    return False


def _sha256(path: Path) -> str:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024 * 1024:
        raise FixtureError("pinned native binary is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _namespace_inode_hash(path: Path) -> str:
    try:
        inode = path.stat().st_ino
    except OSError as exc:
        raise FixtureError("network namespace identity is unavailable") from exc
    return hashlib.sha256(str(inode).encode("ascii")).hexdigest()


def _cap_eff() -> int:
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
    except OSError as exc:
        raise FixtureError("process capability status is unavailable") from exc
    match = re.search(r"^CapEff:\s*([0-9a-fA-F]+)$", status, re.MULTILINE)
    if not match:
        raise FixtureError("effective capabilities could not be read")
    return int(match.group(1), 16)


def _validate_owned_context(role: str, profile: dict[str, Any]) -> dict[str, Any]:
    if role not in {"client", "endpoint"}:
        raise FixtureError("only the fixed client and endpoint roles are implemented")
    if not DOCKER_MARKER.is_file() or not SOURCE_ROOT.is_dir() or not _tmpfs_is_bounded():
        raise FixtureError("packet fixture requires the isolated hosted container layout")
    for marker in PRODUCTION_MARKERS:
        if marker.exists():
            raise FixtureError("packet fixture refuses a production marker")
    if not _mount_is_read_only(PROFILE_PATH):
        raise FixtureError("packet fixture profile is not mounted read-only")
    profile_info = PROFILE_PATH.lstat()
    if profile_info.st_mode & 0o222 or not stat.S_ISREG(profile_info.st_mode):
        raise FixtureError("packet fixture profile is not regular and read-only")
    if (type(profile.get("profile_owner_uid")) is not int
            or profile["profile_owner_uid"] != profile_info.st_uid):
        raise FixtureError("packet fixture profile owner UID does not match its read-only file owner")
    block = profile.get(PROFILE_BLOCK)
    if (profile.get("schema") != PROFILE_SCHEMA or profile.get("profile") != PROFILE_NAME
            or not isinstance(profile.get("suite_nonce"), str)
            or not _NONCE.fullmatch(profile["suite_nonce"])
            or not isinstance(block, dict) or set(block) != {"role", "host_netns_inode_sha256"}
            or block.get("role") != role or not isinstance(block.get("host_netns_inode_sha256"), str)
            or not _SHA256.fullmatch(block["host_netns_inode_sha256"])):
        raise FixtureError("packet fixture profile does not match its closed role contract")
    if _namespace_inode_hash(Path("/proc/self/ns/net")) == block["host_netns_inode_sha256"]:
        raise FixtureError("packet fixture network namespace matches the host namespace")
    if _cap_eff() != NET_ADMIN_BIT:
        raise FixtureError("packet fixture requires NET_ADMIN as its only effective capability")
    xray = profile.get("xray")
    if (not isinstance(xray, dict) or xray.get("path") != str(XRAY_PATH)
            or not isinstance(xray.get("sha256"), str) or not _SHA256.fullmatch(xray["sha256"])
            or _sha256(XRAY_PATH) != xray["sha256"]):
        raise FixtureError("packet fixture Xray binary differs from its parent profile pin")
    return block


_OWNED_ROOT_CREATED = False
_OWNED_MARKER_CREATED = False
_XRAY_CONFIG_CREATED = False


def _bootstrap_owned_root() -> None:
    global _OWNED_ROOT_CREATED, _OWNED_MARKER_CREATED
    try:
        os.mkdir(OWNED_ROOT, 0o700)
        _OWNED_ROOT_CREATED = True
    except FileExistsError as exc:
        raise FixtureError("packet fixture temporary root already exists and is not owned by this run") from exc
    marker = OWNED_ROOT / OWNED_MARKER
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    _OWNED_MARKER_CREATED = True
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(OWNED_MARKER_CONTENT)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise FixtureError("packet fixture ownership marker could not be created") from exc
    os.chmod(marker, 0o400, follow_symlinks=False)


def _cleanup_owned_root() -> None:
    global _OWNED_ROOT_CREATED, _OWNED_MARKER_CREATED, _XRAY_CONFIG_CREATED
    if _XRAY_CONFIG_CREATED:
        (OWNED_ROOT / "xray-endpoint.json").unlink(missing_ok=True)
        _XRAY_CONFIG_CREATED = False
    if _OWNED_MARKER_CREATED:
        (OWNED_ROOT / OWNED_MARKER).unlink(missing_ok=True)
        _OWNED_MARKER_CREATED = False
    if _OWNED_ROOT_CREATED:
        OWNED_ROOT.rmdir()
        _OWNED_ROOT_CREATED = False


def _validate_owned_root() -> None:
    marker_path = OWNED_ROOT / OWNED_MARKER
    root_info = OWNED_ROOT.lstat()
    marker_info = marker_path.lstat()
    if (OWNED_ROOT.is_symlink() or not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != 0
            or root_info.st_gid != 0
            or root_info.st_mode != 0o40700 or marker_path.is_symlink()
            or not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != 0 or marker_info.st_gid != 0
            or marker_info.st_mode & 0o777 != 0o400 or marker_info.st_size != len(OWNED_MARKER_CONTENT)
            or marker_path.read_bytes() != OWNED_MARKER_CONTENT):
        raise FixtureError("packet fixture temporary root ownership marker is unsafe")


def _validate_role_network(role: str) -> None:
    """Require only the fixed role addresses and the fixed return/default route."""
    address_result = _run_fixed(("ip", "-json", "-4", "address", "show"))
    route_result = _run_fixed(("ip", "-json", "-4", "route", "show", "table", "main"))
    if address_result.returncode or route_result.returncode:
        raise FixtureError("fixed role address/route facts could not be read")
    try:
        addresses = json.loads(address_result.stdout)
        routes = json.loads(route_result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FixtureError("fixed role address/route facts were not valid JSON") from exc
    found: set[tuple[str, int]] = set()
    vip_interfaces: set[str] = set()
    if not isinstance(addresses, list):
        raise FixtureError("fixed role address facts have an invalid shape")
    for interface in addresses:
        if not isinstance(interface, dict) or not isinstance(interface.get("addr_info"), list):
            raise FixtureError("fixed role address facts have an invalid interface")
        for item in interface["addr_info"]:
            if (isinstance(item, dict) and item.get("family") == "inet"
                    and isinstance(item.get("local"), str) and isinstance(item.get("prefixlen"), int)):
                found.add((item["local"], item["prefixlen"]))
                if item["local"] == SERVICE_VIP:
                    vip_interfaces.add(str(interface.get("ifname", "")))
    if role == "client":
        required_addresses = {("127.0.0.1", 8), (CLIENT_IP, 29)}
        required_route = ("default", ROUTER_LAN_IP)
    else:
        required_addresses = {("127.0.0.1", 8), (ENDPOINT_WAN_IP, 29), (SERVICE_VIP, 32)}
        required_route = (CLIENT_NET, ROUTER_WAN_IP)
    if found != required_addresses:
        raise FixtureError("role network addresses differ from the fixed packet topology")
    if role == "endpoint" and vip_interfaces != {"lo"}:
        raise FixtureError("service VIP must be owned only by endpoint loopback")
    if not isinstance(routes, list):
        raise FixtureError("fixed role route facts have an invalid shape")
    route_pairs = {(item.get("dst", "default"), item.get("gateway")) for item in routes if isinstance(item, dict)}
    if required_route not in route_pairs:
        raise FixtureError("role network return/default route differs from the fixed packet topology")
    if role == "client" and route_pairs != {("default", ROUTER_LAN_IP), (CLIENT_NET, None)}:
        raise FixtureError("client role has an unreviewed IPv4 route")
    if role == "endpoint" and route_pairs != {(WAN_NET, None), (CLIENT_NET, ROUTER_WAN_IP)}:
        raise FixtureError("endpoint role has an unreviewed IPv4 route")


def _run_fixed(argv: tuple[str, ...], *, input_bytes: bytes | None = None,
               timeout: float = CONTROL_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    """Run only module-owned constant commands, with bounded time and output."""
    if (not argv or any(not isinstance(part, str) for part in argv)
            or input_bytes is not None and len(input_bytes) > MAX_OUTPUT):
        raise FixtureError("invalid fixed fixture command")
    child: subprocess.Popen[bytes] | None = None
    deadline = time.monotonic() + timeout
    try:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                                 start_new_session=True, env=FIXED_ENV, cwd=OWNED_ROOT)
        if input_bytes is not None:
            assert child.stdin is not None
            child.stdin.write(input_bytes)
            child.stdin.close()
        assert child.stdout is not None and child.stderr is not None
        streams = (child.stdout, child.stderr)
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
        collected = {child.stdout: bytearray(), child.stderr: bytearray()}
        with selectors.DefaultSelector() as selector:
            for stream in streams:
                selector.register(stream, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("fixed fixture command timed out")
                events = selector.select(min(remaining, 0.25))
                if not events and child.poll() is None:
                    continue
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), MAX_OUTPUT + 1 - len(collected[key.fileobj]))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    collected[key.fileobj].extend(chunk)
                    if len(collected[key.fileobj]) > MAX_OUTPUT:
                        raise FixtureError("fixture command output exceeded its byte limit")
        returncode = child.wait(timeout=max(0.01, deadline - time.monotonic()))
        return subprocess.CompletedProcess(argv, returncode, bytes(collected[streams[0]]),
                                           bytes(collected[streams[1]]))
    except FixtureError:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                child.kill()
            child.wait(timeout=2)
        raise
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                child.kill()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        raise FixtureError("bounded fixture command could not complete") from exc


def _install_guard(role: str) -> None:
    """Install the separate per-netns fixture guard before opening listeners."""
    if role not in {"client", "endpoint"}:
        raise FixtureError("no fixture-owned guard policy exists for this role")
    input_rules = [
        f'add rule inet {TABLE_NAME} input meta nfproto ipv6 iifname != "lo" drop',
        f'add rule inet {TABLE_NAME} input iifname "lo" accept',
    ]
    output_rules = [f'add rule inet {TABLE_NAME} output meta nfproto ipv6 oifname != "lo" drop']
    if role == "client":
        input_rules.append(
            f'add rule inet {TABLE_NAME} input ip saddr {ROUTER_LAN_IP} ip daddr {CLIENT_IP} '
            f'tcp dport {CLIENT_CONTROL_PORT} accept')
        input_rules.append(
            f'add rule inet {TABLE_NAME} input ip saddr {CLIENT_IP} ip daddr {CLIENT_IP} '
            f'tcp dport {CLIENT_CONTROL_PORT} accept')
        output_rules.extend((
            f'add rule inet {TABLE_NAME} output ip daddr {LEAK_DNS_TARGET} '
            'counter name leak_dns_stub drop',
            f'add rule inet {TABLE_NAME} output ip daddr {LEAK_RESERVED_TARGET} '
            f'tcp dport {LEAK_RESERVED_PORT} counter name leak_reserved drop',
        ))
    output_rules.extend((
        f'add rule inet {TABLE_NAME} output ip daddr 127.0.0.11 drop',
        f'add rule inet {TABLE_NAME} output oifname "lo" accept',
    ))
    if role == "client":
        input_rules.append(
            f'add rule inet {TABLE_NAME} input ip saddr {{ {ROUTER_LAN_IP}, {ENDPOINT_WAN_IP}, {SERVICE_VIP} }} '
            'ct state established,related accept')
        output_rules.append(
            f'add rule inet {TABLE_NAME} output ip daddr {{ {ROUTER_LAN_IP}, {CLIENT_IP}, {ENDPOINT_WAN_IP}, {SERVICE_VIP} }} '
            'ct state established,related accept')
        output_rules.extend((
            f'add rule inet {TABLE_NAME} output ip daddr {ENDPOINT_WAN_IP} tcp dport 22 accept',
            f'add rule inet {TABLE_NAME} output ip daddr {CLIENT_IP} tcp dport {CLIENT_CONTROL_PORT} accept',
            f'add rule inet {TABLE_NAME} output ip daddr {ENDPOINT_WAN_IP} tcp dport {ENDPOINT_XRAY_PORT} accept',
            f'add rule inet {TABLE_NAME} output ip daddr {SERVICE_VIP} tcp dport {HTTP_PORT} accept',
            f'add rule inet {TABLE_NAME} output ip daddr {SERVICE_VIP} '
            f'udp dport {{ {UDP_ECHO_PORT}, {DNS_PORT} }} accept',
        ))
    else:
        allowed_sources = f"{{ {CLIENT_IP}, {ROUTER_WAN_IP}, {ENDPOINT_WAN_IP}, {SERVICE_VIP} }}"
        input_rules.append(
            f'add rule inet {TABLE_NAME} input ip saddr {allowed_sources} ct state established,related accept')
        input_rules.extend((
            f'add rule inet {TABLE_NAME} input ip saddr {allowed_sources} ip daddr {ENDPOINT_WAN_IP} '
            f'tcp dport {{ {ENDPOINT_XRAY_PORT}, {ENDPOINT_CONTROL_PORT} }} accept',
            f'add rule inet {TABLE_NAME} input ip saddr {allowed_sources} ip daddr {SERVICE_VIP} '
            f'tcp dport {HTTP_PORT} accept',
            f'add rule inet {TABLE_NAME} input ip saddr {allowed_sources} ip daddr {SERVICE_VIP} '
            f'udp dport {{ {UDP_ECHO_PORT}, {DNS_PORT} }} accept',
        ))
        output_rules.extend((
            f'add rule inet {TABLE_NAME} output ip daddr {allowed_sources} ct state established,related accept',
            f'add rule inet {TABLE_NAME} output ip daddr {ENDPOINT_WAN_IP} '
            f'tcp dport {{ {ENDPOINT_XRAY_PORT}, {ENDPOINT_CONTROL_PORT} }} accept',
            f'add rule inet {TABLE_NAME} output ip daddr {SERVICE_VIP} tcp dport {HTTP_PORT} accept',
            f'add rule inet {TABLE_NAME} output ip daddr {SERVICE_VIP} '
            f'udp dport {{ {UDP_ECHO_PORT}, {DNS_PORT} }} accept',
        ))
    definitions = [f"add table inet {TABLE_NAME}"]
    if role == "client":
        definitions.extend((f"add counter inet {TABLE_NAME} leak_dns_stub",
                            f"add counter inet {TABLE_NAME} leak_reserved"))
    script = "\n".join((*definitions, *(
        f"add chain inet {TABLE_NAME} input {{ type filter hook input priority 0; policy drop; }}",
        f"add chain inet {TABLE_NAME} output {{ type filter hook output priority 0; policy drop; }}",
        f"add chain inet {TABLE_NAME} forward {{ type filter hook forward priority 0; policy drop; }}",
        f"add rule inet {TABLE_NAME} forward meta nfproto ipv6 drop",
        *input_rules, *output_rules, "",
    ))).encode("ascii")
    result = _run_fixed(("nft", "-f", "-"), input_bytes=script)
    if result.returncode:
        raise FixtureError("fixture egress guard installation failed: " + _redact(result.stderr))


def _remove_guard() -> None:
    result = _run_fixed(("nft", "delete", "table", "inet", TABLE_NAME))
    if result.returncode:
        raise FixtureError("fixture-owned egress guard cleanup failed")


def _guard_counter(name: str) -> int:
    if name not in {"leak_dns_stub", "leak_reserved"}:
        raise FixtureError("unknown fixed fixture counter")
    result = _run_fixed(("nft", "-j", "list", "counter", "inet", TABLE_NAME, name))
    if result.returncode:
        raise FixtureError("fixture egress guard counter is unavailable")
    try:
        payload = json.loads(result.stdout)
        entries = [item["counter"] for item in payload.get("nftables", [])
                   if isinstance(item, dict) and isinstance(item.get("counter"), dict)
                   and item["counter"].get("name") == name]
        if len(entries) != 1 or not isinstance(entries[0].get("packets"), int):
            raise ValueError("counter shape")
        return entries[0]["packets"]
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FixtureError("fixture egress guard counter output is invalid") from exc


def _expected_xray_config() -> dict[str, Any]:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": ENDPOINT_WAN_IP, "port": ENDPOINT_XRAY_PORT, "protocol": "vless",
            "tag": "fixture-vless", "settings": {
                "clients": [{"id": XRAY_ID, "level": 0}], "decryption": "none",
            }, "streamSettings": {"network": "tcp"},
        }],
        "outbounds": [{"protocol": "freedom", "tag": "fixture-egress"}],
        "routing": {"rules": []},
    }


def _assert_xray_config(config: dict[str, Any]) -> None:
    """Reject config drift that could add listeners, users, or external egress."""
    if config != _expected_xray_config():
        raise FixtureError("endpoint Xray routing rules are not permitted")


class _Observations:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {"http": 0, "udp": 0, "dns": 0}
        self._peers: dict[str, list[str]] = {"http": [], "udp": [], "dns": []}
        self._last_peers: dict[str, str | None] = {"http": None, "udp": None, "dns": None}

    def record(self, kind: str, peer: str) -> None:
        with self._lock:
            self._counts[kind] += 1
            self._last_peers[kind] = peer
            peers = self._peers[kind]
            if peer not in peers and len(peers) < 8:
                peers.append(peer)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counts": dict(self._counts),
                "peers": {key: list(value) for key, value in self._peers.items()},
                "last_peers": dict(self._last_peers),
            }


_OBS = _Observations()
_ROLE_STOP = threading.Event()
_HEALTH_LOCK = threading.Lock()
_HEALTH_AVAILABLE = True


def _health_available() -> bool:
    with _HEALTH_LOCK:
        return _HEALTH_AVAILABLE


def _set_health_available(available: bool) -> None:
    global _HEALTH_AVAILABLE
    with _HEALTH_LOCK:
        _HEALTH_AVAILABLE = available


def _tcp_listener_present(address: str, port: int) -> bool:
    wanted_address = format(int.from_bytes(socket.inet_aton(address), "little"), "08X")
    wanted_port = f"{port:04X}"
    try:
        lines = Path("/proc/net/tcp").read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return False
    return any(len(fields := line.split()) > 3 and fields[1] == f"{wanted_address}:{wanted_port}"
               and fields[3] == "0A" for line in lines if line)


def _server_socket_ready(server: Any, address: str, port: int, *, tcp: bool) -> bool:
    try:
        if server.server_address != (address, port):
            return False
        if tcp:
            return server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
        return server.socket.getsockname() == (address, port)
    except OSError:
        return False


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    server_version = ""
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(data) > MAX_JSON_RESPONSE:
            self.send_error(500)
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


class _EndpointHandler(_QuietHandler):
    def do_GET(self) -> None:
        is_control = self.server.server_address == (ENDPOINT_WAN_IP, ENDPOINT_CONTROL_PORT)
        if is_control and self.path == "/ready":
            runtime = getattr(self.server, "xray_runtime", None)
            self._send_json(200, _endpoint_status(self.server, runtime))
        elif is_control and self.path == "/observations":
            self._send_json(200, _OBS.snapshot())
        elif self.server.server_address == (SERVICE_VIP, HTTP_PORT) and self.path == "/generate_204":
            self._send_generate_204()
        elif is_control and self.path == "/xray":
            runtime = getattr(self.server, "xray_runtime", None)
            if runtime is None:
                self._send_json(503, {"ready": False})
            else:
                self._send_json(200, runtime.snapshot())
        elif self.path == "/probe" and self.server.server_address[0] == SERVICE_VIP:
            _OBS.record("http", self.client_address[0])
            self._send_json(200, {"ok": True, "peer": self.client_address[0]})
        else:
            self._send_json(404, {"error": "not_found"})

    def do_HEAD(self) -> None:
        if self.server.server_address == (SERVICE_VIP, HTTP_PORT) and self.path == "/generate_204":
            self._send_generate_204()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

    def _send_generate_204(self) -> None:
        status = 204 if _health_available() else 503
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self) -> None:
        if (self.server.server_address != (ENDPOINT_WAN_IP, ENDPOINT_CONTROL_PORT)
                or self.path != "/health"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if self.headers.get("Transfer-Encoding"):
            self._send_json(400, {"error": "body_not_allowed"})
            return
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdigit() or int(length) > 64:
            self._send_json(400, {"error": "invalid_body_length"})
            return
        try:
            body = self.rfile.read(int(length))
            payload = json.loads(body)
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid_body"})
            return
        if not isinstance(payload, dict) or set(payload) != {"available"} or type(payload["available"]) is not bool:
            self._send_json(400, {"error": "invalid_health_state"})
            return
        _set_health_available(payload["available"])
        self._send_json(200, {"available": _health_available()})


class _ClientHandler(_QuietHandler):
    def do_GET(self) -> None:
        if self.path == "/ready":
            self._send_json(200, {"ready": _server_socket_ready(self.server, CLIENT_IP,
                                                                    CLIENT_CONTROL_PORT, tcp=True),
                                  "role": "client", "sockets": {"control_tcp_8081": True}})
        elif self.path == "/observations":
            self._send_json(200, _OBS.snapshot())
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path not in {"/probe/tcp", "/probe/udp", "/probe/dns", "/probe/dns-negative",
                             "/probe/leak", "/probe/router-forward-leak"}:
            self._send_json(404, {"error": "not_found"})
            return
        length = self.headers.get("Content-Length", "0")
        if length != "0" or self.headers.get("Transfer-Encoding"):
            self._send_json(400, {"error": "body_not_allowed"})
            return
        try:
            if self.path == "/probe/tcp":
                result = _probe_http()
            elif self.path == "/probe/udp":
                result = _probe_udp()
            elif self.path == "/probe/dns":
                result = _probe_dns()
            elif self.path == "/probe/dns-negative":
                result = _probe_dns_negative()
            elif self.path == "/probe/router-forward-leak":
                result = _probe_router_forward_leak()
            else:
                result = _probe_leak()
        except (OSError, FixtureError, TimeoutError) as exc:
            self._send_json(502, {"ok": False, "error": type(exc).__name__})
            return
        observation_kind = {
            "/probe/tcp": "http",
            "/probe/udp": "udp",
            "/probe/dns": "dns",
        }.get(self.path)
        if observation_kind is not None:
            _OBS.record(observation_kind, result.get("peer", SERVICE_VIP))
        self._send_json(200, {"ok": True, **result})


class _BoundedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8
    _slots = threading.BoundedSemaphore(8)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, address = super().get_request()
        request.settimeout(CONTROL_TIMEOUT)
        return request, address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _BoundedUDPServer(socketserver.UDPServer):
    allow_reuse_address = False
    request_queue_size = 8


class _UDPEchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        if len(data) != len(PROBE_BODY) or data != PROBE_BODY:
            return
        _OBS.record("udp", self.client_address[0])
        sock.sendto(PROBE_BODY, self.client_address)


def _dns_name_wire(name: str) -> bytes:
    labels = name.rstrip(".").split(".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise FixtureError("fixed DNS name is invalid")
    encoded = b"".join(bytes((len(label),)) + label.encode("ascii") for label in labels)
    if len(encoded) > 250:
        raise FixtureError("fixed DNS name exceeds the DNS wire limit")
    return encoded + b"\0"


def _dns_query(name: str = DNS_NAME) -> bytes:
    import secrets
    identifier = secrets.randbits(16)
    return (identifier.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
            + _dns_name_wire(name) + b"\x00\x01\x00\x01")


def _dns_response(query: bytes, peer: str) -> bytes:
    if len(query) > 512 or len(query) < 17:
        return b""
    identifier = query[:2]
    if query[2:4] != b"\x01\x00" or query[4:6] != b"\x00\x01" or query[6:12] != b"\0" * 6:
        return b""
    question_end = query.find(b"\0", 12)
    if question_end < 13 or question_end + 5 > len(query):
        return b""
    question = query[12:question_end + 5]
    qtype_qclass = query[question_end + 1:question_end + 5]
    if qtype_qclass != b"\x00\x01\x00\x01":
        return identifier + b"\x81\x04\x00\x00\x00\x00\x00\x00\x00\x00"
    requested_name = query[12:question_end + 1]
    name_wire = _dns_name_wire(DNS_NAME)
    if requested_name != name_wire:
        return identifier + b"\x81\x03\x00\x01\x00\x00\x00\x00\x00\x00" + question
    _OBS.record("dns", peer)
    answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x1e\x00\x04" + socket.inet_aton(DNS_IP)
    return identifier + b"\x81\x00\x00\x01\x00\x01\x00\x00\x00\x00" + question + answer


class _DNSHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        query, sock = self.request
        response = _dns_response(query, self.client_address[0])
        if response:
            sock.sendto(response, self.client_address)


def _probe_http() -> dict[str, Any]:
    with socket.create_connection((SERVICE_VIP, HTTP_PORT), timeout=CONTROL_TIMEOUT) as conn:
        conn.settimeout(CONTROL_TIMEOUT)
        conn.sendall(b"GET /probe HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n")
        data = bytearray()
        while len(data) <= MAX_HTTP_BODY:
            chunk = conn.recv(min(512, MAX_HTTP_BODY + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_HTTP_BODY or b" 200 " not in data[:64] or b"\r\n\r\n" not in data:
            raise FixtureError("bounded HTTP probe received an invalid response")
        _, body = bytes(data).split(b"\r\n\r\n", 1)
        try:
            response = json.loads(body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise FixtureError("bounded HTTP probe response was not valid JSON") from exc
        if not isinstance(response, dict):
            raise FixtureError("bounded HTTP probe response was not an object")
        peer = response.get("peer")
        if response.get("ok") is not True or peer not in {CLIENT_IP, ROUTER_WAN_IP, SERVICE_VIP}:
            raise FixtureError("bounded HTTP probe did not prove a fixed fixture peer")
        return {"service": "http", "response_bytes": len(data), "peer": peer}


def _probe_udp() -> dict[str, Any]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(CONTROL_TIMEOUT)
        sock.sendto(PROBE_BODY, (SERVICE_VIP, UDP_ECHO_PORT))
        data, peer = sock.recvfrom(128)
        if data != PROBE_BODY or peer[0] != SERVICE_VIP or peer[1] != UDP_ECHO_PORT:
            raise FixtureError("bounded UDP probe received an invalid response")
    return {"service": "udp", "peer": peer[0]}


def _probe_dns() -> dict[str, Any]:
    query = _dns_query()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(CONTROL_TIMEOUT)
        sock.sendto(query, (SERVICE_VIP, DNS_PORT))
        data, peer = sock.recvfrom(512)
    if (len(data) < 12 or data[:2] != query[:2] or data[2:4] != b"\x81\x00"
            or data[4:6] != b"\x00\x01" or data[6:8] != b"\x00\x01"
            or data[-4:] != socket.inet_aton(DNS_IP)):
        raise FixtureError("bounded DNS probe received an invalid fixed answer")
    return {"service": "dns", "peer": peer[0], "answer": DNS_IP}


def _probe_dns_negative() -> dict[str, Any]:
    query = _dns_query(DNS_NEGATIVE_NAME)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(CONTROL_TIMEOUT)
        sock.sendto(query, (SERVICE_VIP, DNS_PORT))
        data, peer = sock.recvfrom(512)
    if (len(data) < 12 or data[:2] != query[:2] or data[2:4] != b"\x81\x03"
            or data[4:6] != b"\x00\x01" or data[6:8] != b"\x00\x00"):
        raise FixtureError("fixed unknown .test DNS name was not answered with NXDOMAIN")
    return {"service": "dns-negative", "peer": peer[0], "rcode": "NXDOMAIN"}


def _probe_leak() -> dict[str, Any]:
    before_dns = _guard_counter("leak_dns_stub")
    before_reserved = _guard_counter("leak_reserved")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(CONTROL_TIMEOUT)
        sock.sendto(_dns_query(), (LEAK_DNS_TARGET, 53))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((LEAK_RESERVED_TARGET, LEAK_RESERVED_PORT))
        except OSError:
            pass
        else:
            raise FixtureError("fixed negative egress probe unexpectedly connected")
    after_dns = _guard_counter("leak_dns_stub")
    after_reserved = _guard_counter("leak_reserved")
    if after_dns <= before_dns or after_reserved <= before_reserved:
        raise FixtureError("fixed negative probes were not rejected by the owned egress guard")
    return {"service": "egress-guard", "blocked": True,
            "stub_drop_packets": after_dns - before_dns,
            "reserved_drop_packets": after_reserved - before_reserved}


def _probe_router_forward_leak() -> dict[str, Any]:
    """Probe TCP/22, which Core classifies direct even in global VPN mode."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((ENDPOINT_WAN_IP, 22))
        except OSError:
            return {"service": "router-forward-guard", "blocked": True}
        raise FixtureError("fixed router-forward TCP/22 negative probe unexpectedly connected")


class _XrayRuntime:
    def __init__(self, process: subprocess.Popen[bytes], binary_sha256: str) -> None:
        self.process = process
        self.binary_sha256 = binary_sha256
        self._lock = threading.Lock()
        self._stderr = bytearray()
        if process.stderr is None:
            raise FixtureError("pinned endpoint Xray stderr capture is unavailable")
        self._reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self.role_servers: tuple[Any, ...] = ()

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while True:
            try:
                chunk = os.read(self.process.stderr.fileno(), 1024)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._stderr.extend(chunk)
                if len(self._stderr) > MAX_OUTPUT:
                    del self._stderr[:-MAX_OUTPUT]

    def stderr_tail(self) -> str:
        with self._lock:
            return _redact(bytes(self._stderr), limit=2048)

    def snapshot(self) -> dict[str, Any]:
        return {"running": self.running, "exit_code": self.process.poll(),
                "binary_sha256": self.binary_sha256, "stderr_tail": self.stderr_tail()}

    def terminate(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.process.stderr is not None:
            self.process.stderr.close()
        self._reader.join(timeout=2)


def _start_xray(profile: dict[str, Any]) -> _XrayRuntime:
    xray = profile.get("xray")
    if (not isinstance(xray, dict) or xray.get("path") != str(XRAY_PATH)
            or not isinstance(xray.get("sha256"), str) or not _SHA256.fullmatch(xray["sha256"])
            or _sha256(XRAY_PATH) != xray["sha256"]):
        raise FixtureError("endpoint Xray binary does not match its parent profile pin")
    global _XRAY_CONFIG_CREATED
    xray_config = OWNED_ROOT / "xray-endpoint.json"
    if xray_config.exists() or xray_config.is_symlink():
        raise FixtureError("endpoint Xray config path already exists")
    config = _expected_xray_config()
    _assert_xray_config(config)
    data = (json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    fd = os.open(xray_config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    _XRAY_CONFIG_CREATED = True
    with os.fdopen(fd, "wb", closefd=True) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(xray_config, 0o400, follow_symlinks=False)
    stored_config = _read_json(xray_config)
    if stored_config != config:
        raise FixtureError("generated endpoint Xray config changed before validation")
    checked = _run_fixed((str(XRAY_PATH), "run", "-test", "-config", str(xray_config)), timeout=10.0)
    if checked.returncode:
        raise FixtureError("pinned endpoint Xray config test failed: " + _redact(checked.stderr))
    try:
        process = subprocess.Popen((str(XRAY_PATH), "run", "-config", str(xray_config)),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                                   env=FIXED_ENV, cwd=OWNED_ROOT)
        return _XrayRuntime(process, xray["sha256"])
    except OSError as exc:
        raise FixtureError("pinned endpoint Xray process could not start") from exc


def _wait_tcp(address: str, port: int, deadline: float, runtime: _XrayRuntime | None = None) -> None:
    while time.monotonic() < deadline:
        if runtime is not None and not runtime.running:
            raise FixtureError("endpoint Xray exited before its listener became ready")
        try:
            with socket.create_connection((address, port), timeout=0.25):
                return
        except OSError:
            # Selector wait avoids fixed sleeps and keeps the poll bounded.
            with selectors.DefaultSelector() as selector:
                selector.select(min(0.1, max(0.0, deadline - time.monotonic())))
    raise FixtureError("fixed fixture listener did not become ready before its deadline")


def _endpoint_status(control_server: Any, runtime: _XrayRuntime | None) -> dict[str, Any]:
    servers = getattr(control_server, "role_servers", ())
    sockets = {
        "xray_tcp_5301": _tcp_listener_present(ENDPOINT_WAN_IP, ENDPOINT_XRAY_PORT),
        "http_tcp_9080": bool(servers and _server_socket_ready(servers[0], SERVICE_VIP, HTTP_PORT, tcp=True)),
        "control_tcp_8082": _server_socket_ready(control_server, ENDPOINT_WAN_IP,
                                                   ENDPOINT_CONTROL_PORT, tcp=True),
        "udp_echo_9081": bool(len(servers) > 2 and _server_socket_ready(
            servers[2], SERVICE_VIP, UDP_ECHO_PORT, tcp=False)),
        "dns_udp_5353": bool(len(servers) > 3 and _server_socket_ready(
            servers[3], SERVICE_VIP, DNS_PORT, tcp=False)),
    }
    running = runtime is not None and runtime.running
    return {"ready": running and all(sockets.values()), "role": "endpoint", "sockets": sockets,
            "xray": runtime.snapshot() if runtime else {}}


def _serve(role: str, profile: dict[str, Any]) -> None:
    if role == "endpoint":
        runtime = _start_xray(profile)
        servers: list[Any] = []
        serving: list[Any] = []
        try:
            servers.append(_BoundedHTTPServer((SERVICE_VIP, HTTP_PORT), _EndpointHandler))
            servers.append(_BoundedHTTPServer((ENDPOINT_WAN_IP, ENDPOINT_CONTROL_PORT), _EndpointHandler))
            servers.append(_BoundedUDPServer((SERVICE_VIP, UDP_ECHO_PORT), _UDPEchoHandler))
            servers.append(_BoundedUDPServer((SERVICE_VIP, DNS_PORT), _DNSHandler))
            servers[1].xray_runtime = runtime
            runtime.role_servers = tuple(servers)
            deadline = time.monotonic() + READINESS_TIMEOUT
            _wait_tcp(ENDPOINT_WAN_IP, ENDPOINT_XRAY_PORT, deadline, runtime)
            for server in servers:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
                thread.start()
                serving.append(server)
            _wait_tcp(ENDPOINT_WAN_IP, ENDPOINT_CONTROL_PORT, deadline, runtime)
            _wait_tcp(SERVICE_VIP, HTTP_PORT, deadline, runtime)
            watcher = threading.Thread(target=lambda: (runtime.process.wait(), _ROLE_STOP.set()), daemon=True)
            watcher.start()
            _ROLE_STOP.wait()
            if runtime.process.poll() is not None:
                status = runtime.snapshot()
                raise FixtureError("endpoint Xray exited unexpectedly: " +
                                   json.dumps(status, sort_keys=True, separators=(",", ":")))
        finally:
            for server in servers:
                if server in serving:
                    server.shutdown()
                server.server_close()
            runtime.terminate()
    elif role == "client":
        server = _BoundedHTTPServer((CLIENT_IP, CLIENT_CONTROL_PORT), _ClientHandler)
        try:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            _ROLE_STOP.wait()
        finally:
            server.shutdown()
            server.server_close()
    else:
        raise FixtureError("only the fixed client and endpoint roles are implemented")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("client", "endpoint"))
    args = parser.parse_args(argv)
    guard_installed = False

    def stop_role(signum: int, frame: Any) -> None:
        _ROLE_STOP.set()

    try:
        _ROLE_STOP.clear()
        signal.signal(signal.SIGTERM, stop_role)
        signal.signal(signal.SIGINT, stop_role)
        profile = _read_json(PROFILE_PATH, require_root_owned=False)
        _validate_owned_context(args.role, profile)
        _bootstrap_owned_root()
        _validate_owned_root()
        if _ROLE_STOP.is_set():
            return 0
        _validate_role_network(args.role)
        if _ROLE_STOP.is_set():
            return 0
        _install_guard(args.role)
        guard_installed = True
        _serve(args.role, profile)
    except FixtureError as exc:
        print(_redact(str(exc)), file=sys.stderr)
        return 2
    except Exception as exc:
        print(_redact("packet fixture failed: " + type(exc).__name__), file=sys.stderr)
        return 2
    finally:
        if guard_installed:
            try:
                _remove_guard()
            except FixtureError as exc:
                print(_redact(str(exc)), file=sys.stderr)
        try:
            _cleanup_owned_root()
        except OSError as exc:
            print(_redact("owned fixture cleanup failed: " + str(exc)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
