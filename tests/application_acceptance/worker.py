"""Minimal real uvicorn worker used by the hosted process acceptance suite."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import socket
import sys
import threading
import traceback
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any


def _clean_environment() -> None:
    keep = {
        "PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
        "FWROUTER_STATE_DIR", "FWROUTER_ENVIRONMENT", "FWROUTER_STARTUP_TASKS_ENABLED",
        "FWROUTER_ACCEPTANCE_RPC_SOCKET", "FWROUTER_ACCEPTANCE_PROFILE",
        "FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL",
        "FWROUTER_ACCEPTANCE_MIHOMO_PROBE_URL", "FWROUTER_ACCEPTANCE_MIHOMO_PROBE_PORT",
        "FWROUTER_APPLICATION_ACCEPTANCE_ROOT", "FWROUTER_ACCEPTANCE_RECEIPT_PATH",
        "FWROUTER_XRAY_BINARY", "FWROUTER_MIHOMO_BINARY", "FWROUTER_BROWSER_EXECUTABLE",
        "FWROUTER_CHROMIUM_BINARY", "HOME", "TMPDIR",
    }
    clean = {key: os.environ[key] for key in keep if key in os.environ}
    os.environ.clear()
    os.environ.update(clean)
    os.environ["FWROUTER_ENVIRONMENT"] = "test"
    os.environ["FWROUTER_STARTUP_TASKS_ENABLED"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


_PACKET_DNSMASQ_LOCK = threading.RLock()
_PACKET_DNSMASQ_STATE: dict[str, Any] = {}


def _select_owned_core_candidate(
    payload: dict[str, Any], *, environment: str | None, acceptance_root: str | None,
    profile: dict[str, Any], selector: Any,
) -> dict[str, Any]:
    """Run a test-scoped candidate choice through the real Core selector callable."""
    if (environment != "test" or acceptance_root != "/tmp/fwrouter-application-acceptance"
            or profile.get("profile") != "hosted-kernel-dataplane"
            or not isinstance(payload, dict) or set(payload) != {"candidate_server_id"}):
        return {"ok": False, "error_code": "ACCEPTANCE_CORE_SELECTOR_REQUEST_INVALID"}
    raw_candidate = payload.get("candidate_server_id")
    if type(raw_candidate) is int and raw_candidate > 0:
        candidate_server_id = str(raw_candidate)
    elif (isinstance(raw_candidate, str) and 0 < len(raw_candidate) <= 128
          and all(char.isascii() and (char.isalnum() or char in "._:-") for char in raw_candidate)):
        candidate_server_id = raw_candidate
    else:
        return {"ok": False, "error_code": "ACCEPTANCE_CORE_SELECTOR_CANDIDATE_INVALID"}
    result = selector(
        apply=True, reason="acceptance_provider_baseline", requested_by="api",
        check_on_demand=True, update_ping_state=False, on_demand_limit=20,
        timeout_ms=1000, candidate_server_id=candidate_server_id, origin="api",
    )
    summary: dict[str, Any] = {
        "ok": result.get("ok") is True,
        "selected_server_id": result.get("selected_server_id"),
        "active_after": result.get("active_after"),
        "selection_revision": result.get("selection_revision"),
        "applied": result.get("applied") is True,
        "selection_outcome": result.get("selection_outcome"),
    }
    error_code = result.get("error_code")
    if isinstance(error_code, str) and len(error_code) <= 80:
        summary["error_code"] = error_code
    operation = result.get("provider_operation")
    if isinstance(operation, dict):
        summary["provider_operation"] = {
            key: operation.get(key)
            for key in ("ok", "runtime_verified", "actual_member_id", "error_code")
            if key in operation
        }
    return summary


def _drain_dnsmasq_pipe(stream: Any, output: bytearray) -> None:
    try:
        while True:
            chunk = stream.read(512)
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > 2048:
                del output[:-2048]
    except OSError:
        return


def _dnsmasq_native_uid_matches(process: Any) -> bool:
    pid = getattr(process, "pid", None)
    if type(pid) is not int or pid <= 1:
        return False
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except OSError:
        return False
    match = re.search(r"^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$", status, re.MULTILINE)
    return bool(match and {int(value) for value in match.groups()} == {os.geteuid()})


def _dnsmasq_diagnostic_tail(runtime: Any) -> str | None:
    raw = runtime.get("diagnostic") if isinstance(runtime, dict) else None
    if not isinstance(raw, bytearray) or not raw:
        return None
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"(?i)(password|secret|token|authorization)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"https?://[^\s]+", "[URL]", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b", "[ADDRESS]", text)
    text = re.sub(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b", "[HOST]", text)
    text = re.sub(r"/(?:[^\s:]|:(?!\d))+", "[PATH]", text)
    return text.encode("utf-8", "replace")[-512:].decode("utf-8", "replace")


def _packet_dnsmasq_fixture(state: Path, profile: dict[str, Any], *, stop: bool = False) -> dict[str, Any]:
    """Use production DNS generation with owned paths and the pinned real daemon."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"
            or profile.get("profile") != "hosted-kernel-packet" or os.geteuid() != 0):
        raise RuntimeError("packet DNS fixture requires the qualified isolated packet worker")
    network = profile.get("network_testbed")
    if not isinstance(network, dict) or network.get("role") != "router":
        raise RuntimeError("packet DNS fixture requires the qualified router role")
    host_hash = network.get("host_netns_inode_sha256")
    if (not isinstance(host_hash, str)
            or hashlib.sha256(str(os.stat("/proc/self/ns/net").st_ino).encode("ascii")).hexdigest() == host_hash):
        raise RuntimeError("packet DNS fixture refused outside the owned network namespace")

    with _PACKET_DNSMASQ_LOCK:
        current = _PACKET_DNSMASQ_STATE
        if stop:
            if not current:
                return {"stopped": True, "process_was_owned": False}
            runtime = current.get("runtime")
            process = runtime.get("process") if isinstance(runtime, dict) else None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except Exception:
                    process.kill()
                    process.wait(timeout=2)
            module = current.get("module")
            for name, value in (current.get("paths") or {}).items():
                setattr(module, name, value)
            if module is not None and current.get("subprocess_module") is not None:
                module.subprocess = current["subprocess_module"]
            settings = current.get("settings")
            if settings is not None:
                settings.lan_interface_allowlist = current.get("lan_allowlist", [])
            root = current.get("root")
            cleaned = False
            if isinstance(root, Path) and root == Path(current.get("state")) / "dns53-owned" and not root.is_symlink():
                config_dir = root / "dnsmasq.d"
                root_info = root.lstat()
                try:
                    config_info = config_dir.lstat()
                except FileNotFoundError:
                    config_info = None
                if (not root.is_dir() or root_info.st_uid != os.getuid() or root_info.st_mode & 0o077
                        or (config_info is not None and (config_dir.is_symlink() or not config_dir.is_dir()
                            or config_info.st_uid != os.getuid() or config_info.st_mode & 0o077))):
                    raise RuntimeError("packet DNS teardown refused unowned private directories")
                if config_info is not None:
                    for name in ("fwrouter-rules.conf", "fwrouter-dhcp-dns.conf",
                                 "fwrouter-ipv6-lan.conf", "fwrouter-local-hosts.conf"):
                        item = config_dir / name
                        try:
                            info = item.lstat()
                        except FileNotFoundError:
                            continue
                        if item.is_symlink() or not item.is_file() or info.st_uid != os.getuid():
                            raise RuntimeError("packet DNS teardown refused an unowned config artifact")
                        item.unlink()
                pid_file = root / "dnsmasq.pid"
                try:
                    pid_info = pid_file.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if pid_file.is_symlink() or not pid_file.is_file() or pid_info.st_uid != os.getuid():
                        raise RuntimeError("packet DNS teardown refused an unowned PID artifact")
                    pid_file.unlink()
                if config_info is not None:
                    config_dir.rmdir()
                root.rmdir()
                cleaned = True
            _PACKET_DNSMASQ_STATE.clear()
            reader = runtime.get("reader") if isinstance(runtime, dict) else None
            if reader is not None:
                reader.join(timeout=1)
            return {"stopped": True, "process_was_owned": process is not None,
                    "process_reaped": process is None or process.poll() is not None,
                    "owned_paths_cleaned": cleaned}

        if current:
            runtime = current.get("runtime")
            process = runtime.get("process") if isinstance(runtime, dict) else None
            return {"configured": current.get("configured") is True,
                    "daemon_running": process is not None and process.poll() is None,
                    "daemon_uid_matches": process is not None and _dnsmasq_native_uid_matches(process),
                    "dnsmasq_version": current.get("dnsmasq_version"),
                    "dnsmasq_nftset_support": current.get("nftset_support") is True,
                    "lan_interface_count": 1}

        import subprocess
        from types import SimpleNamespace
        from fwrouter_api.core.config import get_settings
        from fwrouter_api.services import dnsmasq

        root = state / "dns53-owned"
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RuntimeError("packet DNS config root already exists and cannot be adopted") from exc
        info = root.lstat()
        if root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("packet DNS config root is not private and test-owned")
        # Register ownership before any command or module-global override can
        # fail, so the fixture's normal stop/finally path can always unwind it.
        current.update({"state": state, "root": root, "runtime": {"process": None},
                        "configured": False})
        config_dir = root / "dnsmasq.d"
        config_dir.mkdir(mode=0o700)

        ip_result = subprocess.run(["/usr/sbin/ip", "-j", "-4", "address", "show"],
                                   check=False, capture_output=True, timeout=3, stdin=subprocess.DEVNULL,
                                   env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "TZ": "UTC"})
        if ip_result.returncode != 0 or len(ip_result.stdout) > 8192 or len(ip_result.stderr) > 1024:
            raise RuntimeError("packet DNS interface readback failed or exceeded its bound")
        addresses = json.loads(ip_result.stdout)
        lan_rows = []
        wan_rows = []
        for interface in addresses if isinstance(addresses, list) else []:
            if not isinstance(interface, dict) or not isinstance(interface.get("ifname"), str):
                continue
            for item in interface.get("addr_info") or []:
                if not isinstance(item, dict) or item.get("family") != "inet":
                    continue
                pair = (interface["ifname"], item.get("local"), item.get("prefixlen"))
                if pair[1:] == ("10.240.0.1", 29):
                    lan_rows.append(pair)
                if pair[1:] == ("198.18.240.1", 29):
                    wan_rows.append(pair)
        if (len(lan_rows) != 1 or len(wan_rows) != 1 or lan_rows[0][0] == wan_rows[0][0]
                or any(not isinstance(row[0], str) or not row[0] for row in (*lan_rows, *wan_rows))):
            raise RuntimeError("packet DNS LAN/WAN interface identity is not the fixed topology")

        settings = get_settings()
        old_allowlist = list(settings.lan_interface_allowlist)
        path_names = ("DNSMASQ_RULES_CONF_PATH", "DNSMASQ_DHCP_DNS_CONF_PATH",
                      "DNSMASQ_IPV6_LAN_CONF_PATH", "DNSMASQ_LOCAL_HOSTS_CONF_PATH")
        old_paths = {name: getattr(dnsmasq, name) for name in path_names}
        new_paths = {
            "DNSMASQ_RULES_CONF_PATH": config_dir / "fwrouter-rules.conf",
            "DNSMASQ_DHCP_DNS_CONF_PATH": config_dir / "fwrouter-dhcp-dns.conf",
            "DNSMASQ_IPV6_LAN_CONF_PATH": config_dir / "fwrouter-ipv6-lan.conf",
            "DNSMASQ_LOCAL_HOSTS_CONF_PATH": config_dir / "fwrouter-local-hosts.conf",
        }
        runtime: dict[str, Any] = current["runtime"]
        current.update({"module": dnsmasq, "subprocess_module": dnsmasq.subprocess,
                        "settings": settings, "lan_allowlist": old_allowlist, "paths": old_paths})
        settings.lan_interface_allowlist = [lan_rows[0][0]]
        for name, value in new_paths.items():
            setattr(dnsmasq, name, value)

        current["nftset_support"] = True

        binary = "/usr/sbin/dnsmasq"
        pid_file = root / "dnsmasq.pid"
        arguments = [binary, "--no-daemon", "--user=root", "--group=root", "--no-resolv", "--no-hosts", "--conf-file=/dev/null",
                     "--port=53", "--listen-address=127.0.0.1,10.240.0.1", "--bind-interfaces",
                     "--server=1.1.1.1", f"--pid-file={pid_file}", f"--conf-dir={config_dir}"]
        version_result = subprocess.run([binary, "--version"], check=False, capture_output=True,
                                        timeout=5, stdin=subprocess.DEVNULL,
                                        env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C", "TZ": "UTC"})
        version_text = version_result.stdout.decode("ascii", "strict") if len(version_result.stdout) <= 4096 else ""
        if version_result.returncode != 0 or not version_text.startswith("Dnsmasq version 2.90"):
            raise RuntimeError("packet DNS daemon version differs from its pinned package")
        version_lines = version_text.splitlines()
        if len(version_lines) < 2 or "nftset" not in version_lines[1].split():
            raise RuntimeError("pinned dnsmasq lacks native nftset support")

        def restart_owned_dnsmasq() -> subprocess.CompletedProcess[bytes]:
            test_result = subprocess.run([*arguments, "--test"], check=False, capture_output=True,
                                         timeout=5, stdin=subprocess.DEVNULL,
                                         env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C", "TZ": "UTC"})
            if test_result.returncode != 0 or len(test_result.stdout) > 2048 or len(test_result.stderr) > 2048:
                raise subprocess.CalledProcessError(1, [*arguments, "--test"],
                                                    output=b"", stderr=b"native dnsmasq config test failed")
            prior = runtime.get("process")
            if prior is not None and prior.poll() is None:
                prior.terminate()
                try:
                    prior.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    prior.kill()
                    prior.wait(timeout=2)
                prior_reader = runtime.get("reader")
                if prior_reader is not None:
                    prior_reader.join(timeout=1)
            diagnostic = bytearray()
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
                                        env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C", "TZ": "UTC"},
                                        cwd=str(root))
            reader = threading.Thread(target=_drain_dnsmasq_pipe, args=(process.stdout, diagnostic), daemon=True)
            reader.start()
            runtime.update({"process": process, "diagnostic": diagnostic, "reader": reader})
            if not _dnsmasq_native_uid_matches(process):
                runtime["startup_error_code"] = "native_uid_mismatch"
                raise subprocess.CalledProcessError(1, arguments, output=b"",
                                                    stderr=b"native dnsmasq UID differs from its packet namespace owner")
            monotonic = __import__("time").monotonic
            deadline = monotonic() + 3.0
            backoff = 0.05
            while __import__("time").monotonic() < deadline:
                if process.poll() is not None:
                    reader.join(timeout=0.5)
                    runtime["startup_error_code"] = "native_process_exited"
                    raise subprocess.CalledProcessError(1, arguments, output=b"",
                                                        stderr=b"native dnsmasq exited before readiness")
                try:
                    remaining = max(0.05, deadline - monotonic())
                    probe = subprocess.run(["/usr/bin/dig", "@127.0.0.1", "vpn.test", "A", "+short",
                                            "+time=1", "+tries=1"], check=False, capture_output=True,
                                           timeout=min(0.75, remaining), stdin=subprocess.DEVNULL,
                                           env={"PATH": "/usr/bin:/bin", "LANG": "C", "TZ": "UTC"})
                except subprocess.TimeoutExpired:
                    runtime["startup_error_code"] = "readiness_probe_timeout"
                    break
                if probe.returncode == 0 and b"198.51.100.77" in probe.stdout:
                    return subprocess.CompletedProcess(["systemctl", "restart", "dnsmasq"], 0, b"", b"")
                # Bounded backoff avoids a hot loop; only the actual DNS answer
                # above satisfies readiness.
                __import__("threading").Event().wait(min(backoff, max(0.0, deadline - monotonic())))
                backoff = min(backoff * 2, 0.4)
            runtime.setdefault("startup_error_code", "readiness_deadline")
            raise subprocess.CalledProcessError(1, arguments, output=b"",
                                                stderr=b"native dnsmasq readiness deadline expired")

        real_run = subprocess.run

        def dnsmasq_run(command: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(command, (list, tuple)) and list(command) == ["systemctl", "restart", "dnsmasq"]:
                return restart_owned_dnsmasq()
            if isinstance(command, (list, tuple)) and command and command[0] == "systemctl":
                raise subprocess.CalledProcessError(1, list(command), output=b"", stderr=b"unowned systemctl action refused")
            return real_run(command, *args, **kwargs)

        current.update({"dnsmasq_version": version_lines[0], "arguments": arguments})
        dnsmasq.subprocess = SimpleNamespace(run=dnsmasq_run, CalledProcessError=subprocess.CalledProcessError)
        current["configured"] = True
        return {"configured": True, "daemon_running": False, "dnsmasq_version": version_lines[0],
                "dnsmasq_nftset_support": True, "lan_interface_count": 1,
                "daemon_uid_matches": False}


def _install_packet_handoff_loopback_alias(profile: dict[str, Any]) -> Any:
    """Own only the production handoff /32 inside the isolated packet router netns."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"
            or profile.get("profile") != "hosted-kernel-packet"):
        raise RuntimeError("packet handoff alias requires the qualified isolated packet worker")
    network = profile.get("network_testbed")
    if not isinstance(network, dict) or network.get("role") != "router":
        raise RuntimeError("packet handoff alias requires the qualified router role")
    current_netns_hash = hashlib.sha256(
        str(os.stat("/proc/self/ns/net").st_ino).encode("ascii")
    ).hexdigest()
    if (not isinstance(network.get("host_netns_inode_sha256"), str)
            or current_netns_hash == network["host_netns_inode_sha256"]):
        raise RuntimeError("packet handoff alias refused outside the owned non-host network namespace")

    import subprocess

    address = "172.18.0.1"
    prefix = f"{address}/32"
    fixed_env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}

    def emit(event: str, **facts: bool) -> None:
        payload = {"schema": "fwrouter-acceptance-handoff-alias/v1", "event": event,
                   "qualified_packet_namespace": True, **facts}
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if len(encoded.encode("ascii")) <= 512:
                sys.stderr.write("FWROUTER_ACCEPTANCE_HANDOFF_ALIAS " + encoded + "\n")
                sys.stderr.flush()
        except Exception:
            pass

    def ip_json(*args: str) -> Any:
        proc = subprocess.run(["ip", "-j", *args], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=3, check=False, env=fixed_env)
        if len(proc.stdout) > 8192 or len(proc.stderr) > 2048 or proc.returncode != 0:
            raise RuntimeError("bounded packet handoff address inspection failed")
        try:
            return json.loads(proc.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("packet handoff address inspection was not valid JSON") from exc

    def address_rows(value: Any) -> list[tuple[str, int, str]]:
        rows: list[tuple[str, int, str]] = []
        for interface in value if isinstance(value, list) else []:
            if not isinstance(interface, dict):
                continue
            name = interface.get("ifname")
            for item in interface.get("addr_info") or []:
                if (isinstance(item, dict) and isinstance(item.get("local"), str)
                        and type(item.get("prefixlen")) is int):
                    rows.append((item["local"], item["prefixlen"], str(name or "")))
        return rows

    before = address_rows(ip_json("-4", "address", "show"))
    if any(row[0] == address for row in before):
        raise RuntimeError("packet handoff alias already exists; refusing to adopt an unowned address")
    added = False

    def remove_owned_alias() -> None:
        rows = address_rows(ip_json("-4", "address", "show"))
        owned = [row for row in rows if row[0] == address]
        if not owned:
            emit("cleanup_verified", loopback_address_absent=True)
            return
        if owned != [(address, 32, "lo")]:
            raise RuntimeError("packet handoff alias ownership changed before cleanup")
        proc = subprocess.run(["ip", "address", "del", prefix, "dev", "lo"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
                              check=False, env=fixed_env)
        if proc.returncode != 0 or len(proc.stdout) > 1024 or len(proc.stderr) > 1024:
            raise RuntimeError("packet handoff alias cleanup failed")
        if any(row[0] == address for row in address_rows(ip_json("-4", "address", "show"))):
            raise RuntimeError("packet handoff alias remained after cleanup")
        emit("cleanup_verified", loopback_address_absent=True)

    try:
        proc = subprocess.run(["ip", "address", "add", prefix, "dev", "lo"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
                              check=False, env=fixed_env)
        if proc.returncode != 0:
            raise RuntimeError("packet handoff alias could not be installed in the owned namespace")
        added = True
        if len(proc.stdout) > 1024 or len(proc.stderr) > 1024:
            raise RuntimeError("packet handoff alias command output exceeded its byte limit")
        after = address_rows(ip_json("-4", "address", "show", "dev", "lo"))
        if after.count((address, 32, "lo")) != 1:
            raise RuntimeError("packet handoff alias readback did not match the exact loopback /32")
        route = ip_json("-4", "route", "get", address)
        if (not isinstance(route, list) or len(route) != 1 or not isinstance(route[0], dict)
                or route[0].get("type") != "local" or route[0].get("dev") != "lo"):
            raise RuntimeError("packet handoff alias route did not resolve locally on loopback")
        emit("install_verified", loopback_address_matches=True, local_route_matches=True)
    except Exception:
        if added:
            remove_owned_alias()
        raise
    return remove_owned_alias


def _owned_logical_health_check_groups(groups: Any, probe_url: str, *,
                                       profile: dict[str, Any] | None = None,
                                       expected_probe_port: int | None = None) -> list[dict[str, Any]]:
    """Keep probes on loopback, except profile-owned isolated handoff endpoints."""
    if not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups):
        raise RuntimeError("acceptance logical health-check groups are malformed")
    parsed = urlsplit(probe_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("acceptance logical health-check target is invalid") from exc
    loopback_target = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                       and port is not None and 1 <= port <= 65535
                       and parsed.path == "/generate_204")
    packet_target = (isinstance(profile, dict)
                     and profile.get("profile") == "hosted-kernel-packet"
                     and parsed.scheme == "http" and parsed.hostname == "203.0.113.53"
                     and port == 9080 and parsed.path == "/generate_204")
    dataplane_handoff = profile.get("mihomo_handoff") if isinstance(profile, dict) else None
    dataplane_target = (
        isinstance(profile, dict) and profile.get("profile") == "hosted-kernel-dataplane"
        and isinstance(dataplane_handoff, dict)
        and set(dataplane_handoff) == {"address", "subnet", "gateway"}
        and parsed.scheme == "http"
        and parsed.hostname == dataplane_handoff.get("address")
        and type(expected_probe_port) is int and 1024 <= expected_probe_port <= 65535
        and port == expected_probe_port
        and parsed.path == "/generate_204"
    )
    if (not (loopback_target or packet_target or dataplane_target) or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise RuntimeError("acceptance logical health-check target is outside its qualified fixed endpoint")
    copied = copy.deepcopy(groups)
    for group in copied:
        group_type = str(group.get("type") or "").strip().lower()
        if group_type in {"fallback", "url-test"} and isinstance(group.get("url"), str):
            group["url"] = probe_url
    return copied


def _rpc_call(socket_path: Path, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = json.dumps({"action": action, "payload": payload}, separators=(",", ":")).encode() + b"\n"
    if len(request) > 16 * 1024:
        raise ValueError("native runner request exceeds limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(25)
        client.connect(str(socket_path))
        client.sendall(request)
        data = bytearray()
        while len(data) <= 4 * 1024 * 1024:
            block = client.recv(65536)
            if not block:
                break
            data.extend(block)
            if b"\n" in data:
                break
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("native runner response exceeds limit")
    response = json.loads(bytes(data).split(b"\n", 1)[0])
    details = response.get("details") if isinstance(response.get("details"), dict) else {}
    if "archive_bytes_b64" in details:
        details["archive_bytes"] = base64.b64decode(details.pop("archive_bytes_b64"), validate=True)
    if "config_bytes_b64" in details:
        details["config_bytes"] = base64.b64decode(details.pop("config_bytes_b64"), validate=True)
    response["details"] = details
    return response


def _acceptance_job_exception_summary(exc: BaseException) -> dict[str, Any]:
    """Project a test-owned apply exception to fixed class and owned source frames."""
    allowed_classes = {
        "AssertionError", "AttributeError", "FileNotFoundError", "ImportError", "IndexError",
        "KeyError", "ModuleNotFoundError", "NotImplementedError", "OSError", "PermissionError",
        "RuntimeError", "TimeoutError", "TypeError", "ValueError", "CalledProcessError",
        "OperationalError", "IntegrityError",
    }
    name = type(exc).__name__
    exception_class = name if name in allowed_classes else "OtherException"
    owned_root = Path("/workspace/backend/fwrouter_api")
    owned_frames: list[dict[str, Any]] = []
    for frame in traceback.extract_tb(exc.__traceback__):
        source = Path(frame.filename)
        if not source.is_absolute():
            continue
        try:
            relative = source.relative_to(owned_root)
        except ValueError:
            continue
        parts = list(relative.with_suffix("").parts)
        if not parts or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", part) for part in parts):
            continue
        module = "fwrouter_api." + ".".join(parts)
        if len(module) > 160:
            continue
        function = frame.name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", function):
            continue
        line = frame.lineno
        if type(line) is not int or line < 1 or line > 1_000_000:
            continue
        owned_frames.append({
            "module": module,
            "function": function,
            "line": line,
        })
    return {
        "exception_class": exception_class,
        "source_frames": owned_frames[-4:] if owned_frames else "outside_fwrouter_api",
    }


def _install_acceptance_apply_exception_observer() -> None:
    """Observe only the fixed packet DNS manual-apply handler and preserve its behavior."""
    from fwrouter_api.jobs.manager import get_default_job_manager

    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("apply exception observer requires the qualified acceptance worker")
    manager = get_default_job_manager()
    original_get_handler = manager._get_handler

    def observed_get_handler(job_type: str) -> Any:
        handler = original_get_handler(job_type)
        if job_type != "apply_mutation" or not callable(handler):
            return handler

        def observed_handler(job: dict[str, Any]) -> Any:
            job_input = job.get("input") if isinstance(job.get("input"), dict) else {}
            if (job_input.get("intent") != "apply_manual_rules"
                    or job.get("requested_by") != "packet-dns53"):
                return handler(job)
            try:
                return handler(job)
            except Exception as exc:
                summary = _acceptance_job_exception_summary(exc)
                print("FWROUTER_ACCEPTANCE_APPLY_JOB_EXCEPTION " + json.dumps({
                    "schema": "fwrouter-acceptance-apply-job-exception/v1",
                    "job_type": "apply_mutation", "intent": "apply_manual_rules",
                    **summary,
                }, sort_keys=True, separators=(",", ":")), flush=True)
                raise

        return observed_handler

    manager._get_handler = observed_get_handler


def _install_acceptance_mihomo_fence_observer() -> None:
    """Capture bounded hash-only evidence when the real publication fence refuses."""
    from fwrouter_api.services import mihomo_reconcile as reconcile

    original_fingerprint = reconcile.current_mihomo_input_fingerprint
    original_fence = reconcile._selection_publication_still_owned
    original_revision = reconcile.read_selection_revision
    original_incarnation = reconcile._mihomo_incarnation
    original_file_hash = reconcile._file_hash
    original_guard_held = reconcile.xray_writer_guard_is_held
    fingerprints: list[dict[str, Any]] = []
    state: dict[str, Any] = {"inside_fence": False, "reads": {}, "emitted": False}

    def leaf_hashes(value: Any, prefix: str = "", output: dict[str, str] | None = None) -> dict[str, str]:
        output = output if output is not None else {}
        if len(output) >= 1024:
            return output
        if isinstance(value, dict) and value:
            for key in sorted(value, key=str):
                leaf_hashes(value[key], f"{prefix}.{key}" if prefix else str(key), output)
        elif isinstance(value, list) and value:
            for index, item in enumerate(value):
                leaf_hashes(item, f"{prefix}[{index}]", output)
        else:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), default=str).encode("utf-8")
            output[prefix or "root"] = hashlib.sha256(encoded).hexdigest()
        return output

    def observed_fingerprint(routing=None):
        result = original_fingerprint(routing)
        if isinstance(result, dict) and isinstance(result.get("payload"), dict):
            row = {"hash": result.get("hash"), "leaves": leaf_hashes(result["payload"]),
                   "inside_fence": bool(state["inside_fence"])}
            fingerprints.append(row)
            if len(fingerprints) > 32:
                del fingerprints[:-32]
            if state["inside_fence"]:
                state["reads"]["input_fingerprint"] = row
        return result

    def observed_revision(connection):
        value = original_revision(connection)
        if state["inside_fence"]:
            state["reads"]["selection_revision"] = value
        return value

    def observed_incarnation():
        value = original_incarnation()
        if state["inside_fence"]:
            state["reads"]["runtime_incarnation"] = value
        return value

    def observed_file_hash(path):
        value = original_file_hash(path)
        if state["inside_fence"]:
            state["reads"]["active_config_hash"] = value
        return value

    def observed_guard_held():
        value = original_guard_held()
        if state["inside_fence"]:
            state["reads"]["writer_guard_held"] = bool(value)
        return value

    def observed_fence(**kwargs):
        state["reads"] = {}
        state["inside_fence"] = True
        try:
            owned = original_fence(**kwargs)
        finally:
            state["inside_fence"] = False
        if owned or state["emitted"]:
            return owned

        expected_input_hash = kwargs.get("expected_input_hash")
        expected = next((row for row in reversed(fingerprints)
                         if row["hash"] == expected_input_hash and not row["inside_fence"]), None)
        current = state["reads"].get("input_fingerprint")
        changed_paths = []
        changed_leaves = []
        if expected and current:
            before, after = expected["leaves"], current["leaves"]
            changed_paths = sorted(path for path in set(before) | set(after)
                                   if before.get(path) != after.get(path))
            changed_leaves = [{"path": path, "expected_sha256": before.get(path),
                               "observed_sha256": after.get(path)}
                              for path in changed_paths[:64]]
        revision = state["reads"].get("selection_revision")
        incarnation = state["reads"].get("runtime_incarnation")
        active_hash = state["reads"].get("active_config_hash")
        verification = kwargs.get("verification")
        checks = {
            "writer_guard_held": state["reads"].get("writer_guard_held"),
            "selection_revision_matches": revision == kwargs.get("expected_revision"),
            "runtime_incarnation_matches": incarnation == kwargs.get("expected_incarnation"),
            "input_fingerprint_matches": bool(current and current.get("hash") == expected_input_hash),
            "active_config_hash_matches": active_hash == kwargs.get("expected_active_hash"),
            "verification_matches": verification is None or bool(
                str(verification.get("operation_id") or "") == str(kwargs.get("operation_id") or "")
                and type(verification.get("selection_revision")) is int
                and verification.get("selection_revision") == kwargs.get("expected_revision")
            ),
        }
        record = {
            "schema": "fwrouter-acceptance-mihomo-publication-fence/v1",
            "result": "refused",
            "checks": checks,
            "expected": {
                "selection_revision": kwargs.get("expected_revision"),
                "runtime_incarnation_sha256": hashlib.sha256(str(kwargs.get("expected_incarnation") or "").encode()).hexdigest(),
                "input_fingerprint_sha256": expected_input_hash,
                "active_config_sha256": kwargs.get("expected_active_hash"),
            },
            "observed": {
                "selection_revision": revision,
                "runtime_incarnation_sha256": hashlib.sha256(str(incarnation or "").encode()).hexdigest(),
                "input_fingerprint_sha256": current.get("hash") if current else None,
                "active_config_sha256": active_hash,
            },
            "fingerprint_changed_leaves": changed_leaves,
            "fingerprint_changed_leaves_truncated": len(changed_paths) > len(changed_leaves),
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 12 * 1024:
            record["fingerprint_changed_leaves"] = changed_leaves[:24]
            record["fingerprint_changed_leaves_truncated"] = True
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_FENCE " + encoded + "\n")
        sys.stderr.flush()
        state["emitted"] = True
        return owned

    reconcile.current_mihomo_input_fingerprint = observed_fingerprint
    reconcile.read_selection_revision = observed_revision
    reconcile._mihomo_incarnation = observed_incarnation
    reconcile._file_hash = observed_file_hash
    reconcile.xray_writer_guard_is_held = observed_guard_held
    reconcile._selection_publication_still_owned = observed_fence


def _install_acceptance_provider_verification_observer() -> None:
    """Record bounded redacted phases for exceptions hidden by provider verification."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("provider verification observer requires the qualified acceptance worker")

    from .native_runner import _redact_diagnostic
    from fwrouter_api.services import provider_managed

    state = {"records": 0}
    record_guard = threading.Lock()

    def record_exception(phase: str, exc: Exception, *, line: int | None = None) -> None:
        with record_guard:
            if state["records"] >= 8:
                return
            state["records"] += 1
        record = {
            "schema": "fwrouter-acceptance-provider-verification-exception/v1",
            "phase": phase,
            "exception_type": type(exc).__name__[:96],
            "message": _redact_diagnostic(str(exc), limit=256),
        }
        if line is not None:
            record["line"] = int(line)
        try:
            sys.stderr.write("FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION "
                             + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def record_observer_installed() -> None:
        with record_guard:
            if state["records"] >= 8:
                return
            state["records"] += 1
        record = {
            "schema": "fwrouter-acceptance-provider-verification-exception/v1",
            "event": "observer_installed",
        }
        try:
            sys.stderr.write("FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION "
                             + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def record_verifier_entered() -> None:
        with record_guard:
            if state["records"] >= 8:
                return
            state["records"] += 1
        record = {
            "schema": "fwrouter-acceptance-provider-verification-exception/v1",
            "event": "verify_enter",
        }
        try:
            sys.stderr.write("FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION "
                             + json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def safe_refresh_summary(value: Any, *, depth: int = 0) -> Any:
        allowed = {
            "ok", "stage", "outcome", "status", "runtime_verified", "intent_saved",
            "last_good_retained", "error_code", "code", "promoted", "container_restarted",
            "applied", "update_available", "reconcile_action", "timed_out", "retained",
            "error", "reconcile", "verification_callback_result", "refresh", "source_outcomes",
        }
        if depth >= 5:
            return None
        if isinstance(value, dict):
            summary: dict[str, Any] = {}
            for key, item in list(value.items())[:32]:
                if str(key) not in allowed:
                    continue
                if isinstance(item, dict):
                    nested = safe_refresh_summary(item, depth=depth + 1)
                    if nested:
                        summary[str(key)] = nested
                elif isinstance(item, list):
                    nested = [safe_refresh_summary(entry, depth=depth + 1) for entry in item[:8]]
                    summary[str(key)] = [entry for entry in nested if entry]
                elif isinstance(item, bool) or (isinstance(item, int) and not isinstance(item, bool)):
                    summary[str(key)] = item
                elif isinstance(item, str) and len(item) <= 80 and all(
                    char.isascii() and (char.isalnum() or char in "_.-") for char in item
                ):
                    summary[str(key)] = item
            return summary
        if isinstance(value, bool) or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        if isinstance(value, str) and len(value) <= 80 and all(
            char.isascii() and (char.isalnum() or char in "_.-") for char in value
        ):
            return value
        return None

    def record_refresh_result(binding: Any, result: Any) -> None:
        with record_guard:
            if state["records"] >= 8:
                return
            state["records"] += 1
        expected_ref = binding.get("source_ref") if isinstance(binding, dict) else None
        refresh = result.get("refresh") if isinstance(result, dict) else None
        batch = refresh.get("batch") if isinstance(refresh, dict) else None
        targeted_ref = batch.get("targeted_source_ref") if isinstance(batch, dict) else None
        record = {
            "schema": "fwrouter-acceptance-provider-verification-exception/v1",
            "event": "refresh_result",
            "targeted_source_match": bool(expected_ref and targeted_ref and expected_ref == targeted_ref),
            "summary": safe_refresh_summary(result),
        }
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) > 2048:
                record["summary"] = {"truncated": True}
                encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            sys.stderr.write("FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION " + encoded + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    original_verify = provider_managed.verify_provider_handoff
    original_refresh = provider_managed._refresh_with_material

    def observed_refresh_with_material(binding: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_refresh(binding, *args, **kwargs)
        record_refresh_result(binding, result)
        return result

    provider_managed._refresh_with_material = observed_refresh_with_material

    def observe_verify_exception(frame: Any, event: str, arg: Any) -> None:
        if event == "exception" and frame.f_code is original_verify.__code__:
            _exc_type, exc, _traceback = arg
            record_exception("provider_managed.verify_provider_handoff", exc, line=frame.f_lineno)

    def scoped_trace(frame: Any, event: str, arg: Any) -> Any:
        observe_verify_exception(frame, event, arg)
        return scoped_trace

    def observed_verify_provider_handoff(*args: Any, **kwargs: Any) -> dict[str, Any]:
        record_verifier_entered()
        previous_trace = sys.gettrace()
        sys.settrace(scoped_trace)
        try:
            return original_verify(*args, **kwargs)
        finally:
            sys.settrace(previous_trace)

    provider_managed.verify_provider_handoff = observed_verify_provider_handoff
    record_observer_installed()


def _install_acceptance_selection_fence_observer() -> None:
    """Capture bounded recovery-fence and selection-CAS branch evidence in the owned worker."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("selection-fence observer requires the qualified acceptance worker")

    from fwrouter_api.services import provider_recovery, selector

    limits = {"capture": 4, "commit": 4, "reconcile": 4}
    counts = {key: 0 for key in limits}
    lock = threading.Lock()

    def record(kind: str, fields: dict[str, Any]) -> None:
        with lock:
            if counts[kind] >= limits[kind]:
                return
            counts[kind] += 1
        safe = {"schema": "fwrouter-acceptance-selection-fence/v1", "event": kind, **fields}
        try:
            encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"))
            if len(encoded) > 1024:
                return
            sys.stderr.write("FWROUTER_ACCEPTANCE_SELECTION_FENCE " + encoded + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    original_capture = provider_recovery._capture_recovery_context

    def observed_capture(*args: Any, **kwargs: Any) -> Any:
        state: dict[str, Any] = {"return_line": None, "context_present": False, "exception_type": None}
        target_code = original_capture.__code__

        with lock:
            capture_trace_allowed = counts["capture"] < limits["capture"]
        if not capture_trace_allowed:
            return original_capture(*args, **kwargs)

        def scoped_trace(frame: Any, event: str, value: Any) -> Any:
            if frame.f_code is not target_code:
                return None
            if event == "line":
                state["return_line"] = int(frame.f_lineno)
            elif event == "exception" and isinstance(value, tuple) and value:
                exception_type = value[0]
                state["exception_type"] = getattr(exception_type, "__name__", "Exception")[:64]
            elif event == "return":
                state["return_line"] = int(frame.f_lineno)
                state["context_present"] = isinstance(value, dict)
            return scoped_trace

        previous_trace = sys.gettrace()
        if previous_trace is not None:
            try:
                result = original_capture(*args, **kwargs)
            except Exception as exc:
                record("capture", {
                    "trace_status": "preexisting_trace",
                    "return_line": None,
                    "context_present": False,
                    "exception_type": type(exc).__name__[:64],
                })
                raise
            record("capture", {
                "trace_status": "preexisting_trace",
                "return_line": None,
                "context_present": isinstance(result, dict),
                "exception_type": None,
            })
            return result
        sys.settrace(scoped_trace)
        try:
            return original_capture(*args, **kwargs)
        finally:
            sys.settrace(previous_trace)
            record("capture", {
                "trace_status": "captured",
                "return_line": state["return_line"],
                "context_present": bool(state["context_present"]),
                "exception_type": state["exception_type"],
            })

    provider_recovery._capture_recovery_context = observed_capture

    original_commit = selector.commit_active_selection

    def observed_commit(connection: Any, **kwargs: Any) -> Any:
        expected_active = kwargs.get("expected_active_server_id")
        selected = kwargs.get("server_id")
        active_matches_selected = (
            str(expected_active) == str(selected) if expected_active is not None and selected is not None else None
        )
        result = original_commit(connection, **kwargs)
        record("commit", {
            "active_matches_selected": active_matches_selected,
            "cas_committed": result is not None,
        })
        return result

    selector.commit_active_selection = observed_commit

    original_reconcile = selector._reconcile_observed_selection_after_cas_miss

    def observed_reconcile(*args: Any, **kwargs: Any) -> Any:
        result = original_reconcile(*args, **kwargs)
        error_code = result.get("error_code") if isinstance(result, dict) else None
        if (not isinstance(error_code, str) or len(error_code) > 96
                or not all(char.isascii() and (char.isalnum() or char in "_.-") for char in error_code)):
            error_code = None
        record("reconcile", {
            "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
            "error_code": error_code,
        })
        return result

    selector._reconcile_observed_selection_after_cas_miss = observed_reconcile


def _install_acceptance_mihomo_reconcile_failure_observer() -> None:
    """Log fixed, secret-free Mihomo failure predicates before Core drops details."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("Mihomo reconcile observer requires the qualified acceptance worker")

    from fwrouter_api.services import apply_orchestrator

    original = apply_orchestrator._build_failure_result
    lock = threading.Lock()
    state = {"records": 0}
    limit = 16
    max_bytes = 4096

    def safe_code(value: Any) -> str | None:
        if (isinstance(value, str) and 0 < len(value) <= 96
                and all(char.isascii() and (char.isalnum() or char in "_.:-") for char in value)):
            return value
        return None

    def safe_flag_map(value: Any, keys: tuple[str, ...]) -> dict[str, bool]:
        if not isinstance(value, dict):
            return {}
        return {key: value[key] for key in keys if type(value.get(key)) is bool}

    def safe_summary(details: Any) -> dict[str, Any] | None:
        if not isinstance(details, dict):
            return None
        reconcile = details.get("mihomo_reconcile")
        if not isinstance(reconcile, dict):
            return None
        candidate = reconcile.get("candidate") if isinstance(reconcile.get("candidate"), dict) else {}
        validation = reconcile.get("config_validation") if isinstance(reconcile.get("config_validation"), dict) else {}
        promoted = reconcile.get("promoted") if isinstance(reconcile.get("promoted"), dict) else {}
        container = reconcile.get("container") if isinstance(reconcile.get("container"), dict) else {}
        native = reconcile.get("native_udp_policy_proof")
        recovery = reconcile.get("generation_recovery")
        if not isinstance(recovery, dict):
            recovery = reconcile.get("rollback") if isinstance(reconcile.get("rollback"), dict) else {}
        summary: dict[str, Any] = {
            "ok": reconcile.get("ok") if type(reconcile.get("ok")) is bool else None,
            "stage": safe_code(reconcile.get("stage")),
            "error_code": safe_code(reconcile.get("error_code")),
            "reconcile_action": safe_code(reconcile.get("reconcile_action")),
            "reconcile_reason": safe_code(reconcile.get("reconcile_reason")),
            "validation": {
                **safe_flag_map(validation, ("ok",)),
                "returncode": validation.get("returncode")
                if type(validation.get("returncode")) is int and -1 <= validation["returncode"] <= 255 else None,
                "error_code": safe_code(validation.get("error_code")),
            },
            "candidate": {
                "present": bool(candidate),
                "rules_count": candidate.get("rules_count")
                if type(candidate.get("rules_count")) is int and 0 <= candidate["rules_count"] <= 10000 else None,
                "handoff_assignments_count": candidate.get("handoff_assignments_count")
                if type(candidate.get("handoff_assignments_count")) is int
                and 0 <= candidate["handoff_assignments_count"] <= 10000 else None,
            },
            "promoted": safe_flag_map(promoted, ("ok", "promoted")),
            "container": safe_flag_map(container, ("ok", "restarted", "started")),
            "native_udp_policy_proof": safe_flag_map(
                native, ("ok", "native_top_level_rules_match", "selector_membership_matches",
                         "guarded_rules_match", "wiring_proven")),
            "recovery": {
                **safe_flag_map(recovery, ("ok", "recovered", "restart", "restored")),
                "reason": safe_code(recovery.get("reason")),
                "error_code": safe_code(recovery.get("error_code")),
            },
            "last_good_retained": reconcile.get("last_good_retained")
            if type(reconcile.get("last_good_retained")) is bool else None,
        }
        return summary

    def observed_failure(*args: Any, **kwargs: Any) -> Any:
        details = kwargs.get("details")
        summary = safe_summary(details)
        if summary is not None:
            with lock:
                allowed = state["records"] < limit
                if allowed:
                    state["records"] += 1
            if allowed:
                try:
                    payload = {"schema": "fwrouter-acceptance-mihomo-reconcile-failure/v1",
                               "event": "failure_result_input", "summary": summary}
                    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                    if len(encoded.encode("ascii")) <= max_bytes:
                        sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_RECONCILE_FAILURE " + encoded + "\n")
                        sys.stderr.flush()
                except Exception:
                    pass
        return original(*args, **kwargs)

    apply_orchestrator._build_failure_result = observed_failure


def _install_acceptance_recovery_apply_observer() -> None:
    """Correlate emergency-recovery phases with bounded outcome/error codes."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("recovery apply observer requires the qualified acceptance worker")

    from fwrouter_api.routes import selector as selector_route
    from fwrouter_api.services import apply_orchestrator as apply
    from fwrouter_api.services import provider_recovery

    lock = threading.Lock()
    local = threading.local()
    state = {"next_operation": 0, "records": 0}
    limit = 32

    def valid_code(value: Any) -> str | None:
        if (isinstance(value, str) and len(value) <= 64
                and all(char.isascii() and (char.isalnum() or char in "_.-") for char in value)):
            return value
        return None

    def safe_summary(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"result_type": type(value).__name__[:64]}
        result: dict[str, Any] = {}
        for key in ("ok", "outcome", "status", "stage", "dataplane_capability", "enforcement_level",
                    "action", "effective_override", "applied", "runtime_verified",
                    "traffic_enforcement_guaranteed", "selection_outcome", "candidates_count"):
            item = value.get(key)
            if isinstance(item, bool) or (type(item) is int and 0 <= item <= 10000):
                result[key] = item
            elif isinstance(item, str) and len(item) <= 80 and all(
                char.isascii() and (char.isalnum() or char in "_.- ") for char in item
            ):
                result[key] = item
        code = valid_code(value.get("error_code"))
        if code:
            result["error_code"] = code
        error = value.get("error")
        if isinstance(error, dict):
            nested_code = valid_code(error.get("code"))
            if nested_code:
                result["error_code"] = nested_code
        # The real Core apply result nests its bounded taxonomy under dataplane.
        # Preserve only code/stage/operation facts; never serialize messages or details.
        dataplane = value.get("dataplane")
        if isinstance(dataplane, dict):
            safe_dataplane: dict[str, Any] = {}
            if isinstance(dataplane.get("ok"), bool):
                safe_dataplane["ok"] = dataplane["ok"]
            for key in ("stage", "operation"):
                item = dataplane.get(key)
                if isinstance(item, str) and len(item) <= 80 and all(
                    char.isascii() and (char.isalnum() or char in "_.- ") for char in item
                ):
                    safe_dataplane[key] = item
            dataplane_code = valid_code(dataplane.get("error_code"))
            if dataplane_code:
                safe_dataplane["error_code"] = dataplane_code
            details = dataplane.get("details")
            if isinstance(details, dict):
                safe_details: dict[str, Any] = {}
                for key in ("stage", "error_stage"):
                    item = details.get(key)
                    if isinstance(item, str) and len(item) <= 80 and all(
                        char.isascii() and (char.isalnum() or char in "_.- ") for char in item
                    ):
                        safe_details[key] = item
                details_code = valid_code(details.get("error_code"))
                if details_code:
                    safe_details["error_code"] = details_code
                failed_enforcement = details.get("failed_runtime_enforcement")
                if isinstance(failed_enforcement, dict):
                    failed_modes = failed_enforcement.get("supported_modes")
                    safe_failed_enforcement: dict[str, Any] = {}
                    for key in ("enforcement_level", "live_global_mode"):
                        item = failed_enforcement.get(key)
                        if isinstance(item, str) and len(item) <= 80 and all(
                            char.isascii() and (char.isalnum() or char in "_.- ") for char in item
                        ):
                            safe_failed_enforcement[key] = item
                    for key in ("traffic_enforcement_guaranteed", "active_mode_matches_intent", "vpn_egress_ready"):
                        item = failed_enforcement.get(key)
                        if isinstance(item, bool):
                            safe_failed_enforcement[key] = item
                    if isinstance(failed_modes, dict) and type(failed_modes.get("vpn")) is bool:
                        safe_failed_enforcement["vpn_supported"] = failed_modes["vpn"]
                    missing = failed_enforcement.get("missing_runtime_requirements")
                    if isinstance(missing, list):
                        safe_failed_enforcement["missing_runtime_requirements"] = [
                            code for code in (valid_code(item) for item in missing[:2]) if code is not None
                        ]
                    if safe_failed_enforcement:
                        safe_details["failed_runtime_enforcement"] = safe_failed_enforcement
                if safe_details:
                    safe_dataplane["details"] = safe_details
            result["dataplane"] = safe_dataplane
        on_demand = value.get("on_demand")
        if isinstance(on_demand, dict):
            counts = {
                key: on_demand[key] for key in ("checked_count", "success_count", "failed_count")
                if type(on_demand.get(key)) is int and 0 <= on_demand[key] <= 10000
            }
            statuses: list[dict[str, str]] = []
            rows = on_demand.get("results")
            if isinstance(rows, list):
                for row in rows[:4]:
                    if not isinstance(row, dict):
                        continue
                    safe_row: dict[str, str] = {}
                    for key in ("status", "error_code"):
                        item = row.get(key)
                        if key == "error_code":
                            item = valid_code(item)
                        if isinstance(item, str) and len(item) <= 80 and all(
                            char.isascii() and (char.isalnum() or char in "_.- ") for char in item
                        ):
                            safe_row[key] = item
                    statuses.append(safe_row)
            result["on_demand"] = {**counts, "results": statuses}
        if "selected_member_id" in value:
            result["selected_member_present"] = bool(value.get("selected_member_id"))
        supported_modes = value.get("supported_modes")
        if isinstance(supported_modes, dict) and type(supported_modes.get("vpn")) is bool:
            result["vpn_supported"] = supported_modes["vpn"]
        preflight = value.get("preflight")
        if isinstance(preflight, dict):
            safe_preflight: dict[str, Any] = {}
            for key in ("can_enforce_global_vpn", "can_prepare_global_vpn", "vpn_policy_installed",
                        "vpn_egress_ready", "vpn_egress_target_present", "vpn_external_path_verified"):
                item = preflight.get(key)
                if isinstance(item, bool):
                    safe_preflight[key] = item
            for key in ("vpn_prepare_missing", "vpn_egress_missing"):
                values = preflight.get(key)
                if isinstance(values, list):
                    safe_preflight[key] = [
                        code for code in (valid_code(item) for item in values[:2]) if code is not None
                    ]
            missing_by_mode = preflight.get("missing_by_mode")
            vpn_missing = missing_by_mode.get("vpn") if isinstance(missing_by_mode, dict) else None
            if isinstance(vpn_missing, list):
                safe_preflight["vpn_missing"] = [
                    code for code in (valid_code(item) for item in vpn_missing[:2]) if code is not None
                ]
            if safe_preflight:
                result["vpn_preflight"] = safe_preflight
        return result

    def record(phase: str, *, operation: int | None = None, result: Any = None,
               exception_type: str | None = None, line: int | None = None) -> None:
        with lock:
            if state["records"] >= limit:
                return
            state["records"] += 1
        payload: dict[str, Any] = {
            "schema": "fwrouter-acceptance-recovery-apply/v1",
            "phase": phase,
            "operation": operation,
        }
        if result is not None:
            payload["result"] = safe_summary(result)
        if exception_type:
            payload["exception_type"] = exception_type[:64]
        if line is not None:
            payload["line"] = int(line)
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if len(encoded) <= 1200:
                sys.stderr.write("FWROUTER_ACCEPTANCE_RECOVERY_APPLY " + encoded + "\n")
                sys.stderr.flush()
        except Exception:
            pass

    def next_operation() -> int:
        with lock:
            state["next_operation"] += 1
            return state["next_operation"]

    original_enter = provider_recovery.enter_emergency_direct
    original_reentry = provider_recovery.try_verified_reentry
    original_apply = provider_recovery._apply_override
    original_apply_under_policy = provider_recovery._apply_override_under_policy
    original_selector_call = selector_route.select_vpn_auto_server
    original_core_apply = apply._run_pipeline_for_state

    def observed_enter(*args: Any, **kwargs: Any) -> Any:
        prior = getattr(local, "operation", None)
        operation = next_operation()
        local.operation = operation
        record("emergency_direct_enter", operation=operation)
        try:
            result = original_enter(*args, **kwargs)
        except Exception as exc:
            record("emergency_direct_exception", operation=operation,
                   exception_type=type(exc).__name__)
            raise
        else:
            record("emergency_direct_result", operation=operation, result=result)
            return result
        finally:
            local.operation = prior

    def observed_reentry(*args: Any, **kwargs: Any) -> Any:
        prior = getattr(local, "operation", None)
        operation = next_operation()
        local.operation = operation
        record("provider_reentry_enter", operation=operation)
        try:
            result = original_reentry(*args, **kwargs)
        except Exception as exc:
            record("provider_reentry_exception", operation=operation,
                   exception_type=type(exc).__name__)
            raise
        else:
            record("provider_reentry_result", operation=operation, result=result)
            return result
        finally:
            local.operation = prior

    def observed_apply(*args: Any, **kwargs: Any) -> Any:
        operation = getattr(local, "operation", None)
        record("override_apply_enter", operation=operation)
        try:
            result = original_apply(*args, **kwargs)
        except Exception as exc:
            record("override_apply_exception", operation=operation,
                   exception_type=type(exc).__name__)
            raise
        record("override_apply_result", operation=operation, result=result)
        return result

    def observed_apply_under_policy(*args: Any, **kwargs: Any) -> Any:
        operation = getattr(local, "operation", None)
        exceptions: list[tuple[str, int]] = []
        prior_trace = sys.gettrace()
        target_code = original_apply_under_policy.__code__
        if prior_trace is None:
            def scoped_trace(frame: Any, event: str, value: Any) -> Any:
                if frame.f_code is not target_code:
                    return None
                if event == "exception" and isinstance(value, tuple) and value:
                    exception_type = getattr(value[0], "__name__", "Exception")
                    if len(exceptions) < 4:
                        exceptions.append((str(exception_type)[:64], int(frame.f_lineno)))
                return scoped_trace
            sys.settrace(scoped_trace)
        record("override_pipeline_enter", operation=operation)
        try:
            result = original_apply_under_policy(*args, **kwargs)
        except Exception as exc:
            record("override_pipeline_exception", operation=operation,
                   exception_type=type(exc).__name__)
            raise
        finally:
            if prior_trace is None:
                sys.settrace(prior_trace)
        for exception_type, line in exceptions:
            record("override_pipeline_caught_exception", operation=operation,
                   exception_type=exception_type, line=line)
        record("override_pipeline_result", operation=operation, result=result)
        return result

    def observed_selector_call(*args: Any, **kwargs: Any) -> Any:
        operation = getattr(local, "operation", None)
        if operation is None:
            operation = next_operation()
        record("core_selector_enter", operation=operation)
        try:
            result = original_selector_call(*args, **kwargs)
        except Exception as exc:
            record("core_selector_exception", operation=operation,
                   exception_type=type(exc).__name__)
            raise
        record("core_selector_result", operation=operation, result=result)
        return result

    def observed_core_apply(*args: Any, **kwargs: Any) -> Any:
        operation = getattr(local, "operation", None)
        if operation is None:
            return original_core_apply(*args, **kwargs)
        try:
            result = original_core_apply(*args, **kwargs)
        except Exception as exc:
            record("core_apply_exception", operation=operation,
                   exception_type=type(exc).__name__)
            raise
        record("core_apply_result", operation=operation, result=result)
        return result

    provider_recovery.enter_emergency_direct = observed_enter
    provider_recovery.try_verified_reentry = observed_reentry
    provider_recovery._apply_override = observed_apply
    provider_recovery._apply_override_under_policy = observed_apply_under_policy
    selector_route.select_vpn_auto_server = observed_selector_call
    apply._run_pipeline_for_state = observed_core_apply


def _install_acceptance_xray_generation_rollback_observer() -> None:
    """Expose bounded rollback receipt predicates before the provider pipeline drops them."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("Xray generation rollback observer requires the qualified acceptance worker")

    from fwrouter_api.services import mihomo_reconcile, xray_subscription_service
    from fwrouter_api.services import mihomo_reconcile_fingerprint

    local = threading.local()
    lock = threading.Lock()
    state = {"records": 0}
    limit = 16
    max_bytes = 2048

    def code(value: Any) -> str | None:
        if (isinstance(value, str) and 0 < len(value) <= 64
                and all(char.isascii() and (char.isalnum() or char in "_.:-") for char in value)):
            return value
        return None

    def emit(event: str, fields: dict[str, Any]) -> None:
        with lock:
            if state["records"] >= limit:
                return
            state["records"] += 1
        payload = {"schema": "fwrouter-acceptance-xray-rollback/v1", "event": event, **fields}
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if len(encoded.encode("ascii")) <= max_bytes:
                sys.stderr.write("FWROUTER_ACCEPTANCE_XRAY_ROLLBACK " + encoded + "\n")
                sys.stderr.flush()
        except Exception:
            pass

    def checkpoint_facts(checkpoint_path: Any, expected_source: Any, expected_revision: Any,
                         expected_runtime: Any, expected_sha: Any) -> dict[str, Any]:
        facts: dict[str, Any] = {"checkpoint_readable": False}
        try:
            path = Path(checkpoint_path)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                return facts
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            artifacts = checkpoint.get("artifacts") if isinstance(checkpoint.get("artifacts"), dict) else {}
            mihomo_artifact = artifacts.get("mihomo_config")
            mihomo_artifact = mihomo_artifact if isinstance(mihomo_artifact, dict) else {}
            encoded = mihomo_artifact.get("text")
            try:
                preimage_matches = bool(
                    isinstance(encoded, str)
                    and hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest() == expected_sha
                )
            except (ValueError, TypeError):
                preimage_matches = False
            facts.update({
                "checkpoint_readable": True,
                "phase_restore_started": checkpoint.get("phase") == "restore_started",
                "source_fingerprint_matches": bool(expected_source)
                and checkpoint.get("source_fingerprint") == expected_source,
                "preimage_hash_matches": preimage_matches,
                "selection_revision_matches": type(expected_revision) is int
                and checkpoint.get("selection_revision") == expected_revision,
                "checkpoint_pre_restore_runtime_matches": bool(expected_runtime)
                and checkpoint.get("mihomo_runtime_incarnation") == expected_runtime,
                "staged_hash_matches": bool(expected_sha)
                and (checkpoint.get("staged_generation") or {}).get("mihomo_final_sha256") == expected_sha,
            })
            from fwrouter_api.services import mihomo_config
            from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation
            active_path = Path(mihomo_config._resolved_base_config_path())
            active_hash = hashlib.sha256(active_path.read_bytes()).hexdigest()
            facts["active_hash_matches"] = active_hash == expected_sha
            facts["runtime_incarnation_matches"] = get_mihomo_runtime_incarnation() == expected_runtime
            fingerprint = mihomo_reconcile_fingerprint.current_mihomo_input_fingerprint()
            facts["input_fingerprint_present"] = bool(
                isinstance(fingerprint, dict) and isinstance(fingerprint.get("hash"), str)
                and bool(fingerprint["hash"])
            )
        except Exception:
            pass
        return facts

    original_restore = xray_subscription_service._restore_xray_generation_checkpoint
    original_rollback_receipt = xray_subscription_service._publish_xray_generation_rollback_mihomo_udp_receipt
    original_publish = xray_subscription_service._publish_mihomo_udp_receipt_with_owner
    original_native_proof = mihomo_reconcile._native_udp_policy_proof
    original_write = mihomo_reconcile_fingerprint.write_mihomo_reconcile_fingerprint_state

    def observed_restore(*args: Any, **kwargs: Any) -> Any:
        prior = getattr(local, "rollback", False)
        local.rollback = True
        try:
            result = original_restore(*args, **kwargs)
        except Exception as exc:
            emit("restore_exception", {"exception_type": type(exc).__name__[:64]})
            raise
        finally:
            local.rollback = prior
        summary: dict[str, Any] = {"result_type": type(result).__name__[:48]}
        if isinstance(result, dict):
            for key in ("ok", "xray_reload", "mihomo_restart", "derived_rows_restored"):
                if type(result.get(key)) is bool:
                    summary[key] = result[key]
            recovered = result.get("recovered")
            if recovered in {"restored_last_good", "restore_failed", "stale_checkpoint_declined"}:
                summary["recovered"] = recovered
            error_code = result.get("error_code")
            if error_code == "MIHOMO_UDP_POLICY_ROLLBACK_RECEIPT_UNCONFIRMED":
                summary["error_code"] = error_code
            for key in ("selection_readback", "xray_runtime_readback", "mihomo_runtime_readback"):
                value = result.get(key)
                if isinstance(value, dict):
                    nested: dict[str, Any] = {}
                    if type(value.get("ok")) is bool:
                        nested["ok"] = value["ok"]
                    reason = code(value.get("reason"))
                    if reason in {"not_checked", "restore_restart_failed"}:
                        nested["reason"] = reason
                    summary[key] = nested
        emit("restore_result", summary)
        return result

    def observed_publish(*, expected_mihomo_runtime: str, expected_mihomo_sha: str,
                         reconcile_reason: str, operation_id: str, owner_matches: Any) -> bool:
        if not getattr(local, "rollback", False) or reconcile_reason != "xray_generation_rollback":
            return original_publish(
                expected_mihomo_runtime=expected_mihomo_runtime,
                expected_mihomo_sha=expected_mihomo_sha,
                reconcile_reason=reconcile_reason, operation_id=operation_id, owner_matches=owner_matches,
            )
        # checkpoint_path and expected source/revision are only available to the
        # outer rollback owner; these owner callback observations remain booleans.
        owner_calls = {"count": 0, "matched": True}

        def observed_owner(fingerprint_hash: str) -> bool:
            matched = bool(owner_matches(fingerprint_hash))
            owner_calls["count"] = min(owner_calls["count"] + 1, 4)
            owner_calls["matched"] = owner_calls["matched"] and matched
            return matched

        result = original_publish(
            expected_mihomo_runtime=expected_mihomo_runtime,
            expected_mihomo_sha=expected_mihomo_sha,
            reconcile_reason=reconcile_reason, operation_id=operation_id, owner_matches=observed_owner,
        )
        emit("receipt_publish_result", {
            "ok": bool(result), "owner_callback_count": owner_calls["count"],
            "owner_callback_all_matched": owner_calls["matched"] if owner_calls["count"] else None,
        })
        return result

    def observed_rollback_receipt(*, checkpoint_path: Any, expected_source: str, expected_revision: int,
                                  expected_mihomo_runtime: str, expected_mihomo_sha: str) -> bool:
        if not getattr(local, "rollback", False):
            return original_rollback_receipt(
                checkpoint_path=checkpoint_path, expected_source=expected_source,
                expected_revision=expected_revision, expected_mihomo_runtime=expected_mihomo_runtime,
                expected_mihomo_sha=expected_mihomo_sha,
            )
        facts = checkpoint_facts(checkpoint_path, expected_source, expected_revision,
                                 expected_mihomo_runtime, expected_mihomo_sha)
        try:
            from fwrouter_api.db.connection import db_session
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
            with db_session() as connection:
                fence = read_selection_fence(connection)
            facts["selection_fence_matches"] = fence.get("revision") == expected_revision
        except Exception:
            facts["selection_fence_matches"] = False
        emit("receipt_owner_preflight", facts)
        result = original_rollback_receipt(
            checkpoint_path=checkpoint_path, expected_source=expected_source,
            expected_revision=expected_revision, expected_mihomo_runtime=expected_mihomo_runtime,
            expected_mihomo_sha=expected_mihomo_sha,
        )
        emit("rollback_receipt_result", {"ok": bool(result)})
        return result

    def observed_native_proof() -> dict[str, Any]:
        result = original_native_proof()
        if getattr(local, "rollback", False):
            proof = result if isinstance(result, dict) else {}
            emit("native_udp_policy_proof", {
                key: proof[key] for key in ("ok", "native_top_level_rules_match",
                                            "selector_membership_matches", "guarded_rules_match")
                if type(proof.get(key)) is bool
            })
        return result

    def observed_write(*args: Any, **kwargs: Any) -> Any:
        result = original_write(*args, **kwargs)
        if getattr(local, "rollback", False):
            summary: dict[str, Any] = {"result_type": type(result).__name__[:48]}
            if type(result) is bool:
                summary["ok"] = result
            emit("receipt_state_write", summary)
        return result

    xray_subscription_service._restore_xray_generation_checkpoint = observed_restore
    xray_subscription_service._publish_xray_generation_rollback_mihomo_udp_receipt = observed_rollback_receipt
    xray_subscription_service._publish_mihomo_udp_receipt_with_owner = observed_publish
    mihomo_reconcile._native_udp_policy_proof = observed_native_proof
    mihomo_reconcile_fingerprint.write_mihomo_reconcile_fingerprint_state = observed_write


def _install_acceptance_generation_callback_observer() -> None:
    """Capture bounded exception provenance at the staged-generation callback seam."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("generation callback observer requires the qualified acceptance worker")

    from .native_runner import _redact_diagnostic
    from fwrouter_api.services import mihomo_reconcile

    state = {"records": 0}
    lock = threading.Lock()
    original_invoke = mihomo_reconcile._invoke_verification_callback

    def record(record: dict[str, Any]) -> None:
        with lock:
            if state["records"] >= 8:
                return
            state["records"] += 1
        record = {
            "schema": "fwrouter-acceptance-generation-callback/v1",
            **record,
        }
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) > 2048:
                return
            sys.stderr.write("FWROUTER_ACCEPTANCE_GENERATION_CALLBACK " + encoded + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def observed_invoke(callback: Any, *, operation_id: str, expected_revision: int) -> Any:
        callback_code = getattr(callback, "__code__", None)
        callback_name = callback_code.co_name if callback_code is not None else type(callback).__name__
        record({"event": "callback_enter", "callback": str(callback_name)[:96]})
        try:
            result = original_invoke(
                callback, operation_id=operation_id, expected_revision=expected_revision,
            )
        except Exception as exc:
            frames: list[dict[str, Any]] = []
            current = exc.__traceback__
            while current is not None:
                code = current.tb_frame.f_code
                frames.append({
                    "function": code.co_name[:96],
                    "file": Path(code.co_filename).name[:96],
                    "line": int(current.tb_lineno),
                })
                current = current.tb_next
            record({
                "event": "callback_exception",
                "callback": str(callback_name)[:96],
                "exception_type": type(exc).__name__[:96],
                "message": _redact_diagnostic(str(exc), limit=256),
                "frames": frames[-3:],
            })
            raise
        summary = {"event": "callback_result", "callback": str(callback_name)[:96]}
        if isinstance(result, dict):
            summary["ok"] = bool(result.get("ok"))
            for key in ("status", "stage", "error_code"):
                value = result.get(key)
                if isinstance(value, str) and len(value) <= 96 and all(
                    char.isascii() and (char.isalnum() or char in "_.- ") for char in value
                ):
                    summary[key] = value
            selector = result.get("selector")
            if isinstance(selector, dict):
                selector_summary: dict[str, Any] = {}
                for key in (
                    "error_code", "selection_outcome", "status", "selection_basis", "reason",
                    "applied", "candidates_count", "selection_revision", "runtime_adapter_id",
                    "checked_count", "success_count", "failed_count", "selector_readback_matches",
                ):
                    value = selector.get(key)
                    if isinstance(value, bool) or (isinstance(value, int) and not isinstance(value, bool)):
                        selector_summary[key] = value
                    elif isinstance(value, str) and len(value) <= 96 and all(
                        char.isascii() and (char.isalnum() or char in "_.- ") for char in value
                    ):
                        selector_summary[key] = value
                reconciliation = selector.get("reconciliation_after_cas_miss")
                if isinstance(reconciliation, dict):
                    reconciliation_summary: dict[str, Any] = {}
                    for key in ("attempted", "confirmed"):
                        if isinstance(reconciliation.get(key), bool):
                            reconciliation_summary[key] = reconciliation[key]
                    outcome = reconciliation.get("outcome")
                    if isinstance(outcome, str) and outcome in {"pending", "repaired", "deferred"}:
                        reconciliation_summary["outcome"] = outcome
                    code = reconciliation.get("error_code")
                    if isinstance(code, str) and len(code) <= 96 and all(
                        char.isascii() and (char.isalnum() or char in "_.-") for char in code
                    ):
                        reconciliation_summary["error_code"] = code
                    selector_summary["reconciliation_after_cas_miss"] = reconciliation_summary
                on_demand = selector.get("on_demand")
                if isinstance(on_demand, dict):
                    on_demand_summary = {
                        key: int(on_demand[key]) for key in ("checked_count", "success_count", "failed_count")
                        if type(on_demand.get(key)) is int
                    }
                    rows = on_demand.get("results")
                    if isinstance(rows, list):
                        safe_rows: list[dict[str, Any]] = []
                        for row in rows[:4]:
                            if not isinstance(row, dict):
                                continue
                            safe_row: dict[str, Any] = {}
                            for key in ("status", "error_code"):
                                value = row.get(key)
                                if isinstance(value, str) and len(value) <= 96 and all(
                                    char.isascii() and (char.isalnum() or char in "_.- ") for char in value
                                ):
                                    safe_row[key] = value
                            message = row.get("error_message")
                            if isinstance(message, str) and message:
                                safe_row["error_message"] = _redact_diagnostic(message, limit=256)
                            safe_rows.append(safe_row)
                        on_demand_summary["results"] = safe_rows
                    selector_summary["on_demand"] = on_demand_summary
                provider_operation = selector.get("provider_operation")
                if isinstance(provider_operation, dict):
                    selector_summary["provider_operation"] = {
                        key: provider_operation[key] for key in ("error_code", "outcome", "runtime_verified")
                        if isinstance(provider_operation.get(key), (str, bool))
                        and (not isinstance(provider_operation.get(key), str)
                             or len(provider_operation[key]) <= 96 and all(
                                 char.isascii() and (char.isalnum() or char in "_.- ")
                                 for char in provider_operation[key]
                             ))
                    }
                summary["selector"] = selector_summary
        else:
            summary["ok"] = bool(result)
        record(summary)
        return result

    mihomo_reconcile._invoke_verification_callback = observed_invoke


def _install_acceptance_mihomo_delay_error_observer() -> None:
    """Capture bounded Mihomo delay error bodies without changing HTTP behavior."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("Mihomo delay observer requires the qualified acceptance worker")

    import re
    import httpx
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from .native_runner import _redact_diagnostic

    original_raise = httpx.Response.raise_for_status
    original_zero_readback = MihomoHttpAdapter._has_fresh_zero_delay_for_url
    state = {"records": 0, "success_samples": 0, "member_state_snapshot_used": False,
             "zero_readback_records": 0}
    lock = threading.Lock()
    url_pattern = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
    endpoint_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")

    def observed_zero_readback(
        proxy: dict[str, Any], *, test_url: str, probe_started_at: Any, observed_end: Any,
    ) -> bool:
        result = original_zero_readback(
            proxy, test_url=test_url, probe_started_at=probe_started_at, observed_end=observed_end,
        )
        try:
            from datetime import datetime, timezone

            extra = proxy.get("extra") if isinstance(proxy, dict) else None
            sample = extra.get(test_url) if isinstance(extra, dict) else None
            sample_present = isinstance(sample, dict)
            alive = sample_present and sample.get("alive") is True
            history = sample.get("history") if sample_present else None
            latest = history[-1] if isinstance(history, list) and history else None
            delay_zero = (
                isinstance(latest, dict)
                and type(latest.get("delay")) is int
                and latest["delay"] == 0
            )
            time_in_window = False
            raw_time = latest.get("time") if isinstance(latest, dict) else None
            if isinstance(raw_time, str) and raw_time.strip():
                try:
                    checked_at = datetime.fromisoformat(raw_time.strip().replace("Z", "+00:00"))
                    if (checked_at.tzinfo is not None and checked_at.utcoffset() is not None
                            and probe_started_at.tzinfo is not None and observed_end.tzinfo is not None):
                        checked_at = checked_at.astimezone(timezone.utc)
                        started = probe_started_at.astimezone(timezone.utc)
                        ended = observed_end.astimezone(timezone.utc)
                        time_in_window = started <= checked_at <= ended
                except (OverflowError, TypeError, ValueError):
                    pass
            with lock:
                if state["zero_readback_records"] >= 8:
                    return result
                state["zero_readback_records"] += 1
            record = {
                "schema": "fwrouter-acceptance-mihomo-zero-readback/v1",
                "extra_url_sample_present": sample_present,
                "sample_alive": alive,
                "latest_delay_zero": delay_zero,
                "history_time_in_probe_window": time_in_window,
                "native_helper_accepted": result is True,
            }
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) <= 512:
                sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_ZERO_READBACK " + encoded + "\n")
                sys.stderr.flush()
        except Exception:
            pass
        return result

    MihomoHttpAdapter._has_fresh_zero_delay_for_url = staticmethod(observed_zero_readback)

    def response_error(response: Any) -> None:
        try:
            request_url = response.request.url
            if request_url.host != "127.0.0.1" or request_url.port != 5200:
                return
            path = str(request_url.path)
            if path.startswith("/group/") and path.endswith("/delay"):
                category = "group_delay"
            elif path.startswith("/proxies/") and path.endswith("/delay"):
                category = "proxy_delay"
            else:
                return
            status = int(response.status_code)
            if status == 200:
                payload: Any = None
                try:
                    payload = json.loads(bytes(response.content[:8192]).decode("utf-8", "replace"))
                except (TypeError, ValueError):
                    pass
                delay_values: list[int] = []
                if isinstance(payload, dict):
                    candidates = payload.values() if category == "group_delay" else (payload.get("delay"),)
                    delay_values = [value for value in candidates
                                    if type(value) is int and 0 <= value <= 65535][:32]
                with lock:
                    if state["success_samples"] >= 4:
                        return
                    state["success_samples"] += 1
                record = {
                    "schema": "fwrouter-acceptance-mihomo-delay-sample/v1",
                    "status": status,
                    "path_category": category,
                }
                if category == "group_delay":
                    record["delays_ms"] = delay_values
                elif delay_values:
                    record["delay_ms"] = delay_values[0]
                encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
                if len(encoded) <= 512:
                    sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_DELAY_SAMPLE " + encoded + "\n")
                    sys.stderr.flush()
                return
            if status < 400 or status > 599:
                return

            if category == "proxy_delay" and status == 503:
                with lock:
                    first_state_snapshot = not state["member_state_snapshot_used"]
                    state["member_state_snapshot_used"] = True
                if first_state_snapshot:
                    record_proxy_state_snapshot(response)

            payload = json.loads(bytes(response.content[:8192]).decode("utf-8", "replace"))
            message: Any = None
            if isinstance(payload, dict):
                message = payload.get("message")
                if not isinstance(message, str):
                    error = payload.get("error")
                    message = error.get("message") if isinstance(error, dict) else error
            if not isinstance(message, str):
                return
            safe_message = _redact_diagnostic(message, limit=512)
            safe_message = endpoint_pattern.sub("[ENDPOINT]", url_pattern.sub("[URL]", safe_message))
            safe_message = _redact_diagnostic(safe_message, limit=256)
            with lock:
                if state["records"] >= 8:
                    return
                state["records"] += 1
            record = {
                "schema": "fwrouter-acceptance-mihomo-delay-error/v1",
                "status": status,
                "path_category": category,
                "error_message": safe_message,
            }
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) > 512:
                return
            sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_DELAY_ERROR " + encoded + "\n")
            sys.stderr.flush()
        except Exception:
            return

    def record_proxy_state_snapshot(response: Any) -> None:
        """Take one bounded read-only state snapshot for the first owned member 503."""
        record: dict[str, Any] = {
            "schema": "fwrouter-acceptance-mihomo-proxy-state/v1",
            "path_category": "proxy_state",
        }
        try:
            from datetime import datetime, timezone
            import httpx

            request_url = response.request.url
            request_path = str(request_url.path)
            if not request_path.startswith("/proxies/") or not request_path.endswith("/delay"):
                return
            state_path = request_path[:-len("/delay")].rstrip("/")
            state_url = request_url.copy_with(path=state_path, query=b"")
            requested_at = datetime.now(timezone.utc)
            with httpx.Client(timeout=1.5, trust_env=False) as client:
                snapshot = client.get(state_url, headers=response.request.headers)
            record["status"] = int(snapshot.status_code)
            record["snapshot_requested_at_utc"] = requested_at.isoformat(timespec="milliseconds")
            if snapshot.status_code == 200:
                body: Any = None
                try:
                    body = json.loads(bytes(snapshot.content[:8192]).decode("utf-8", "replace"))
                except (TypeError, ValueError):
                    pass
                if isinstance(body, dict) and type(body.get("alive")) is bool:
                    record["alive"] = body["alive"]
                history = body.get("history") if isinstance(body, dict) else None
                latest = history[-1] if isinstance(history, list) and history else None
                if isinstance(latest, dict):
                    delay = latest.get("delay")
                    if type(delay) is int and 0 <= delay <= 65535:
                        record["latest_history_delay_ms"] = delay
                    history_time = latest.get("time")
                    if isinstance(history_time, str) and len(history_time) <= 64:
                        try:
                            parsed_time = datetime.fromisoformat(history_time.replace("Z", "+00:00"))
                            if parsed_time.tzinfo is not None:
                                history_utc = parsed_time.astimezone(timezone.utc)
                                record["latest_history_time_utc"] = history_utc.isoformat(timespec="milliseconds")
                                age_ms = int((datetime.now(timezone.utc) - history_utc).total_seconds() * 1000)
                                if abs(age_ms) <= 31_536_000_000:
                                    record["history_age_at_snapshot_ms"] = age_ms
                        except (OverflowError, ValueError):
                            pass
            record["result"] = "response_received"
        except Exception as exc:
            record["result"] = "request_failed"
            record["exception_type"] = type(exc).__name__[:80]
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) <= 1024:
                sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_PROXY_STATE " + encoded + "\n")
                sys.stderr.flush()
        except Exception:
            pass

    def observed_raise_for_status(response: Any) -> Any:
        response_error(response)
        return original_raise(response)

    httpx.Response.raise_for_status = observed_raise_for_status


def build_app(socket_path: Path):
    state = Path(os.environ.get("FWROUTER_STATE_DIR", ""))
    root = Path(os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT", ""))
    if root != Path("/tmp/fwrouter-application-acceptance") or root.is_symlink() or not root.is_dir():
        raise RuntimeError("acceptance worker root is not the fixed orchestrator-owned /tmp root")
    state_real = state.resolve(strict=True)
    root_real = root.resolve(strict=True)
    if not state_real.is_relative_to(root_real) or state.is_symlink():
        raise RuntimeError("application state escapes the owned acceptance root")
    if socket_path.is_symlink() or not socket_path.is_socket() or socket_path.stat().st_uid != os.getuid() or socket_path.stat().st_mode & 0o077:
        raise RuntimeError("native runner socket is not an owned private AF_UNIX socket")
    socket_real = socket_path.resolve(strict=True)
    if not socket_real.is_relative_to(state_real):
        raise RuntimeError("native runner socket escapes the isolated application state")
    from .profile import ProfileError, load_profile
    try:
        acceptance_profile, _ = load_profile(Path(os.environ.get("FWROUTER_ACCEPTANCE_PROFILE", "")))
    except (OSError, ProfileError) as exc:
        raise RuntimeError("acceptance worker profile qualification failed") from exc

    # Import settings before any facade module, and prevent BaseSettings from
    # consulting the deployed absolute dotenv path in a worker process.
    from fwrouter_api.core.config import Settings, get_settings
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()

    from fwrouter_api.adapters.xray import RealXrayAdapter
    from fwrouter_api.adapters.xray_common import XrayApplyResult

    config_path = state / "xray" / "config.json"
    compose_path = state / "xray" / "acceptance-compose.yml"

    def runner(action: str, payload: dict[str, Any]) -> XrayApplyResult:
        reply = _rpc_call(socket_path, action, payload)
        return XrayApplyResult(
            ok=bool(reply.get("ok")), message=str(reply.get("message") or "native Xray action"),
            error_code=reply.get("error_code"), details=reply.get("details") or {},
        )

    adapter = RealXrayAdapter(config_path=config_path, compose_path=compose_path, runner=runner)
    from fwrouter_api.adapters import xray as xray_adapter_module
    from fwrouter_api.services import xray as xray_service
    from fwrouter_api.services import subject_inventory, runtime, xray_runtime_state, xray_status

    xray_adapter_module.DEFAULT_XRAY_ADAPTER = adapter
    xray_service.DEFAULT_XRAY_ADAPTER = adapter
    for module in (subject_inventory, runtime, xray_runtime_state, xray_status):
        if hasattr(module, "DEFAULT_XRAY_ADAPTER"):
            module.DEFAULT_XRAY_ADAPTER = adapter

    from fwrouter_api.jobs.extended_handlers import register_extended_handlers
    from fwrouter_api.jobs.manager import get_default_job_manager
    from fwrouter_api.jobs.handlers import register_default_handlers
    register_default_handlers(get_default_job_manager())
    register_extended_handlers(get_default_job_manager())

    from fwrouter_api.main import create_app
    app = create_app(enable_startup_tasks=False)
    _bind_xray_checkpoint_barrier(socket_path, state)
    mihomo_adapter = _bind_acceptance_mihomo(socket_path, state)
    _install_acceptance_mihomo_fence_observer()
    _install_acceptance_provider_verification_observer()
    _install_acceptance_generation_callback_observer()
    _install_acceptance_mihomo_delay_error_observer()
    _install_acceptance_selection_fence_observer()
    _install_acceptance_mihomo_reconcile_failure_observer()
    _install_acceptance_recovery_apply_observer()
    if acceptance_profile.get("profile") == "hosted-kernel-packet":
        _install_acceptance_xray_generation_rollback_observer()
        _install_acceptance_apply_exception_observer()
    _bind_acceptance_provider()

    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("acceptance controller routes require the qualified test worker")

    def acceptance_provider_recovery_controller() -> Any:
        """Use the same Core runtime controller and selection fence as watchdog recovery."""
        from fwrouter_api.services.watchdog import _watchdog_flow_deps
        from fwrouter_api.services.watchdog_auto_flow import _capture_controller_selection_fence

        deps = _watchdog_flow_deps()
        vpn_adapter = deps.active_watchdog_vpn_adapter()
        controller = deps.get_vpn_runtime_controller(vpn_adapter, routing=deps.load_routing_state())
        _capture_controller_selection_fence(controller)
        if not callable(getattr(controller, "get_state", None)):
            raise RuntimeError("acceptance recovery requires the production Core runtime controller")
        return controller

    # Observe the original Core CAS only after its caller has completed native
    # apply and selector readback. The native parent may hold this transport
    # boundary while a test advances the real persistent revision.
    from fwrouter_api.services import selector as selector_service
    _original_commit_active_selection = selector_service.commit_active_selection

    def commit_active_selection_with_acceptance_barrier(connection: Any, **kwargs: Any) -> int | None:
        provenance = kwargs.get("provenance") if isinstance(kwargs.get("provenance"), dict) else {}
        reply = _rpc_call(socket_path, "selection_commit_barrier", {
            "expected_revision": kwargs.get("expected_revision"),
            "expected_active_server_id": kwargs.get("expected_active_server_id"),
            "expected_provenance_decision_id": kwargs.get("expected_provenance_decision_id"),
            "server_id": kwargs.get("server_id"),
            "operation_id": str(provenance.get("operation_id") or ""),
        })
        if not reply.get("ok"):
            raise RuntimeError("acceptance selection commit barrier transport failed")
        return _original_commit_active_selection(connection, **kwargs)

    selector_service.commit_active_selection = commit_active_selection_with_acceptance_barrier

    def recover_xray_generation(payload: dict[str, Any]) -> dict[str, Any]:
        from fwrouter_api.services.xray_subscription_service import reconcile_xray_subscription_profile_nodes
        options: dict[str, Any] = {}
        if payload.get("verify_selection") is True:
            from fwrouter_api.services.xray_subscription_service import (
                _capture_generation_auto_selection,
                _verify_generation_selection_readback,
            )
            captured = _capture_generation_auto_selection()

            def verify_selection(**context: Any) -> dict[str, Any]:
                # The recovery contract must prove the stored Core fence, not
                # echo caller-supplied values as if they were a successful CAS.
                checkpoint = state / "xray" / ".generation" / "generation-checkpoint.json"
                durable = json.loads(checkpoint.read_text(encoding="utf-8"))
                operation_id = str(durable.get("selection_operation_id") or "")
                expected_revision = durable.get("selection_revision")
                from fwrouter_api.db.connection import db_session
                from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
                with db_session() as connection:
                    actual_fence = read_selection_fence(connection)
                result = _verify_generation_selection_readback(captured)
                context_matches = not context.get("operation_id") or context["operation_id"] == operation_id
                fence_matches = (
                    type(expected_revision) is int
                    and actual_fence["revision"] == expected_revision
                    and captured.get("selection_revision") == expected_revision
                )
                return {
                    **result,
                    "ok": bool(result.get("ok") and fence_matches and context_matches and operation_id),
                    "error_code": result.get("error_code") or (None if fence_matches else "SELECTION_FENCE_CHANGED"),
                    "operation_id": operation_id or None,
                    "selection_revision": actual_fence["revision"],
                    "expected_selection_revision": expected_revision,
                    "selection_fence": actual_fence,
                    "context_operation_id_matches": context_matches,
                }

            options["verification_callback"] = verify_selection
        return reconcile_xray_subscription_profile_nodes(
            requested_by="hosted-acceptance-recovery",
            token_or_slug=str(payload.get("token") or "") or None,
            materialize=True,
            **options,
        )

    app.add_api_route("/api/v2/__acceptance/xray/reconcile", recover_xray_generation, methods=["POST"])

    def wait_for_acceptance_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = payload.get("timeout_seconds", 75)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 90:
            return {"ok": False, "error_code": "ACCEPTANCE_JOB_WAIT_TIMEOUT_INVALID"}
        from fwrouter_api.jobs.manager import get_default_job_manager
        job = get_default_job_manager().wait_for_job(job_id, timeout_seconds=timeout_seconds)
        if job is None:
            return {"ok": False, "error_code": "ACCEPTANCE_JOB_NOT_FOUND"}
        return {"ok": job.get("status") in {"success", "failed", "cancelled"}, "job": job}

    app.add_api_route("/api/v2/__acceptance/jobs/{job_id}/wait", wait_for_acceptance_job, methods=["POST"])

    def advance_selection_revision_for_acceptance(payload: dict[str, Any]) -> dict[str, Any]:
        expected_revision = payload.get("expected_revision")
        if type(expected_revision) is not int or expected_revision < 0:
            return {"ok": False, "error_code": "ACCEPTANCE_SELECTION_REVISION_REQUEST_INVALID"}
        from fwrouter_api.db.connection import db_session
        from fwrouter_api.services.vpn_auto_selection_state import advance_selection_revision, read_selection_fence
        with db_session() as connection:
            before = read_selection_fence(connection)
            if before["revision"] != expected_revision:
                return {"ok": False, "error_code": "ACCEPTANCE_SELECTION_REVISION_STALE", "fence": before}
            revision = advance_selection_revision(connection, expected_revision=expected_revision)
            if revision is None:
                return {"ok": False, "error_code": "ACCEPTANCE_SELECTION_REVISION_STALE"}
            after = read_selection_fence(connection)
        return {"ok": True, "before": before, "fence": after, "revision": revision}

    app.add_api_route("/api/v2/__acceptance/selection/revision/advance", advance_selection_revision_for_acceptance, methods=["POST"])

    def read_selection_fence_for_acceptance() -> dict[str, Any]:
        from fwrouter_api.db.connection import db_session
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        with db_session() as connection:
            return {"ok": True, "fence": read_selection_fence(connection)}

    app.add_api_route("/api/v2/__acceptance/selection/fence", read_selection_fence_for_acceptance, methods=["GET"])

    def select_owned_core_candidate(payload: dict[str, Any]) -> dict[str, Any]:
        """Exercise the real Core selector with one explicit acceptance candidate."""
        from fwrouter_api.services.selector import select_vpn_auto_server
        return _select_owned_core_candidate(
            payload, environment=os.environ.get("FWROUTER_ENVIRONMENT"),
            acceptance_root=os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT"),
            profile=acceptance_profile, selector=select_vpn_auto_server,
        )

    app.add_api_route("/api/v2/__acceptance/selector/candidate", select_owned_core_candidate,
                      methods=["POST"])

    def set_owned_vpn_auto_selector_reject() -> dict[str, Any]:
        """Move only the owned native selector to its fail-closed sentinel for CAS testing."""
        if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
                or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"
                or str(getattr(mihomo_adapter, "base_url", "")) != "http://127.0.0.1:5200"
                or Path(getattr(mihomo_adapter, "config_path", "")).resolve(strict=False)
                != (state / "generated" / "mihomo" / "config.yaml").resolve(strict=False)):
            raise RuntimeError("owned selector fault injection is unavailable outside the qualified Mihomo child")
        before = mihomo_adapter.get_proxy_state("vpn-auto")
        targets = before.get("all") if isinstance(before, dict) and isinstance(before.get("all"), list) else []
        if "REJECT" not in targets:
            return {"ok": False, "error_code": "ACCEPTANCE_REJECT_TARGET_NOT_PRESENT"}
        if before.get("now") == "REJECT":
            return {"ok": False, "error_code": "ACCEPTANCE_REJECT_TARGET_NOT_COMPETING"}
        mihomo_adapter._put_json("/proxies/vpn-auto", {"name": "REJECT"})
        after = mihomo_adapter.get_proxy_state("vpn-auto")
        if not isinstance(after, dict) or after.get("now") != "REJECT":
            return {"ok": False, "error_code": "ACCEPTANCE_REJECT_READBACK_UNCONFIRMED"}
        return {"ok": True, "selector": "vpn-auto", "selected_target": "REJECT",
                "membership_confirmed": True, "readback_confirmed": True}

    app.add_api_route("/api/v2/__acceptance/selection/runtime-reject", set_owned_vpn_auto_selector_reject,
                      methods=["POST"])

    def recover_provider_path(payload: dict[str, Any]) -> dict[str, Any]:
        from fwrouter_api.services.provider_recovery import confirmed_provider_recovery
        logical_server_id = payload.get("logical_server_id")
        decision_id = payload.get("decision_id")
        timeout_ms = payload.get("timeout_ms", 1000)
        allow_switch = payload.get("allow_switch")
        if (not isinstance(logical_server_id, str) or not logical_server_id.strip() or len(logical_server_id) > 128
                or not isinstance(decision_id, str) or not decision_id.strip() or len(decision_id) > 128
                or type(timeout_ms) is not int or not 100 <= timeout_ms <= 5000
                or type(allow_switch) is not bool):
            return {"ok": False, "status": "rejected", "error_code": "ACCEPTANCE_RECOVERY_REQUEST_INVALID"}
        return confirmed_provider_recovery(
            logical_server_id=logical_server_id.strip(), path_key="acceptance",
            decision_id=decision_id.strip(), controller=acceptance_provider_recovery_controller(),
            timeout_ms=timeout_ms, allow_switch=allow_switch,
        ) or {"ok": False, "status": "failed", "error_code": "PROVIDER_RECOVERY_NO_RESULT"}

    app.add_api_route("/api/v2/__acceptance/provider/recovery", recover_provider_path, methods=["POST"])

    def reenter_provider_path(payload: dict[str, Any]) -> dict[str, Any]:
        from fwrouter_api.services.provider_recovery import try_verified_reentry
        timeout_ms = payload.get("timeout_ms", 1000)
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 5000:
            return {"ok": False, "status": "rejected", "error_code": "ACCEPTANCE_REENTRY_REQUEST_INVALID"}
        return try_verified_reentry(acceptance_provider_recovery_controller(),
                                    timeout_ms=timeout_ms, reason="acceptance_reentry")

    app.add_api_route("/api/v2/__acceptance/provider/reentry", reenter_provider_path, methods=["POST"])

    if acceptance_profile.get("profile") == "hosted-kernel-packet":
        def configure_packet_dns53() -> dict[str, Any]:
            fixture = _packet_dnsmasq_fixture(state, acceptance_profile)
            return {
                "ok": fixture.get("configured") is True,
                "fixture_configured": fixture.get("configured") is True,
                "daemon_running": fixture.get("daemon_running") is True,
                "dnsmasq_version": _PACKET_DNSMASQ_STATE.get("dnsmasq_version"),
                "nftset_support": _PACKET_DNSMASQ_STATE.get("nftset_support") is True,
            }

        def packet_dns53_status() -> dict[str, Any]:
            from fwrouter_api.services import dnsmasq
            current = _PACKET_DNSMASQ_STATE
            runtime = current.get("runtime") or {}
            process = runtime.get("process")
            try:
                status = dnsmasq.inspect_dnsmasq_selective_status()
            except Exception:
                status = {}
            rules_path = Path(dnsmasq.DNSMASQ_RULES_CONF_PATH)
            try:
                lines = set(rules_path.read_text(encoding="utf-8").splitlines())
            except OSError:
                lines = set()
            upstream_domains = []
            unexpected_upstream_rules = 0
            for line in sorted(lines):
                if not line.startswith("server=/"):
                    continue
                domain = line[len("server=/"):].split("/", 1)[0]
                if domain in {"vpn.test", "direct.test"}:
                    upstream_domains.append(domain)
                else:
                    unexpected_upstream_rules += 1
            argv_matches = False
            if process is not None and process.poll() is None and type(process.pid) is int and process.pid > 1:
                try:
                    cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes()
                except OSError:
                    cmdline = b""
                if len(cmdline) <= 4096:
                    argv = cmdline.decode("ascii", "ignore").split("\0")
                    argv_matches = (argv[:2] == ["/usr/sbin/dnsmasq", "--no-daemon"]
                                    and "--user=root" in argv and "--group=root" in argv
                                    and "--port=53" in argv and f"--conf-dir={current.get('root') / 'dnsmasq.d'}" in argv)
            nft_probe = (status.get("nftset_probe_status") or {}).get("probes")
            nft_proof = []
            if isinstance(nft_probe, list):
                for item in nft_probe[:4]:
                    if not isinstance(item, dict):
                        continue
                    domain = item.get("domain") if item.get("domain") in {"vpn.test", "direct.test"} else None
                    nft_proof.append({"domain": domain, "ok": item.get("ok") is True,
                                      "resolved": item.get("resolved_ipv4") == ["198.51.100.77"] if domain == "vpn.test" else item.get("resolved_ipv4") == ["203.0.113.53"] if domain == "direct.test" else False,
                                      "materialized_count": len(item.get("materialized")) if isinstance(item.get("materialized"), list) else None})
            return {
                "configured": bool(current.get("configured")),
                "daemon_running": process is not None and process.poll() is None,
                "daemon_uid_matches": process is not None and _dnsmasq_native_uid_matches(process),
                "native_process_arguments_match": argv_matches,
                "selective_status_ok": status.get("ok") is True,
                "dns_capture_ok": (status.get("dns_capture_status") or {}).get("ok") is True,
                "nftset_probe_ok": (status.get("nftset_probe_status") or {}).get("ok") is True,
                "vpn_rule_generated": "server=/vpn.test/1.1.1.1" in lines,
                "upstream_domains": sorted(set(upstream_domains)),
                "unexpected_upstream_rule_count": unexpected_upstream_rules,
                "vpn_nftset_generated": any(line.startswith("nftset=/vpn.test/4#") and line.endswith("#dns_vpn_ipv4") for line in lines),
                "direct_rule_generated": not any(line.startswith("server=/direct.test/") for line in lines),
                "direct_nftset_generated": any(line.startswith("nftset=/direct.test/4#") and line.endswith("#dns_direct_ipv4") for line in lines),
                "nft_probe_targets": nft_proof,
                "process_exit_code": process.returncode if process is not None and process.poll() is not None else None,
                "startup_error_code": runtime.get("startup_error_code") if runtime.get("startup_error_code") in {
                    "native_uid_mismatch", "native_process_exited", "readiness_deadline"} else None,
                "diagnostic_tail": _dnsmasq_diagnostic_tail(runtime),
                "lan_interface_count": 1 if current else 0,
            }

        def stop_packet_dns53() -> dict[str, Any]:
            return _packet_dnsmasq_fixture(state, acceptance_profile, stop=True)

        app.add_api_route("/api/v2/__acceptance/dns53/configure", configure_packet_dns53, methods=["POST"])
        app.add_api_route("/api/v2/__acceptance/dns53/status", packet_dns53_status, methods=["GET"])
        app.add_api_route("/api/v2/__acceptance/dns53/stop", stop_packet_dns53, methods=["POST"])
    return app


def _bind_xray_checkpoint_barrier(socket_path: Path, state: Path) -> None:
    """Expose a barrier after the checkpoint directory fsync has completed."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("Xray checkpoint barriers require the qualified test worker")
    from fwrouter_api.services import xray_subscription_service

    real_fsync_directory = xray_subscription_service._fsync_directory
    checkpoint_path = state / "xray" / ".generation" / "generation-checkpoint.json"

    def fsync_directory_with_checkpoint_barrier(path: Path) -> None:
        real_fsync_directory(path)
        try:
            directory = Path(path).resolve(strict=True)
            if directory != checkpoint_path.parent.resolve(strict=False):
                return
            value = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            phase = str(value.get("phase") or "") if isinstance(value, dict) else ""
        except (OSError, ValueError, TypeError):
            return
        if phase:
            _rpc_call(socket_path, "generation_checkpoint", {"phase": phase})

    xray_subscription_service._fsync_directory = fsync_directory_with_checkpoint_barrier


def _bind_acceptance_mihomo(socket_path: Path, state: Path):
    """Bind the real Mihomo HTTP adapter and process boundary to owned children."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("acceptance Mihomo process binding is available only in the qualified test worker")
    from fwrouter_api.adapters import mihomo as adapter_module
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from fwrouter_api.services import mihomo_runtime, subscription_pipeline
    from fwrouter_api.services import mihomo_config_proxies
    from fwrouter_api.services.mihomo_config_paths import _resolved_candidate_config_path

    expected_candidate = (state / "generated" / "mihomo" / "config.next.yaml").resolve(strict=False)
    default_candidate = Path(_resolved_candidate_config_path()).resolve(strict=False)
    if default_candidate != expected_candidate:
        raise RuntimeError("canonical Mihomo candidate path escapes the owned acceptance state")
    active_config = state / "generated" / "mihomo" / "config.yaml"
    active_contours = state / "generated" / "mihomo" / "contours.json"
    from .profile import load_profile
    acceptance_profile, _ = load_profile()
    profile_mode = acceptance_profile.get("profile")
    packet_profile = profile_mode == "hosted-kernel-packet"
    owned_probe_port = None
    provider_endpoint = os.environ.get("FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL", "").strip()
    parsed_provider_endpoint = urlsplit(provider_endpoint)
    if (parsed_provider_endpoint.scheme != "http" or parsed_provider_endpoint.hostname != "127.0.0.1"
            or parsed_provider_endpoint.port is None or parsed_provider_endpoint.path not in {"", "/"}
            or parsed_provider_endpoint.username or parsed_provider_endpoint.password
            or parsed_provider_endpoint.query or parsed_provider_endpoint.fragment):
        raise RuntimeError("acceptance Mihomo probe target must be the owned loopback provider bridge")

    original_logical_profile_groups = mihomo_config_proxies._logical_profile_groups
    if packet_profile:
        owned_probe_url = "http://203.0.113.53:9080/generate_204"
    elif profile_mode == "hosted-kernel-dataplane":
        owned_probe_url = os.environ.get("FWROUTER_ACCEPTANCE_MIHOMO_PROBE_URL", "")
        raw_probe_port = os.environ.get("FWROUTER_ACCEPTANCE_MIHOMO_PROBE_PORT", "")
        if not re.fullmatch(r"[0-9]{4,5}", raw_probe_port):
            raise RuntimeError("acceptance Mihomo probe port is outside its owned endpoint contract")
        owned_probe_port = int(raw_probe_port)
        _owned_logical_health_check_groups([], owned_probe_url, profile=acceptance_profile,
                                           expected_probe_port=owned_probe_port)
    else:
        owned_probe_url = f"{provider_endpoint}/generate_204"

    def logical_profile_groups_on_owned_bridge() -> list[dict[str, Any]]:
        generated = original_logical_profile_groups()
        return _owned_logical_health_check_groups(
            generated, owned_probe_url, profile=acceptance_profile,
            expected_probe_port=owned_probe_port)

    mihomo_config_proxies._logical_profile_groups = logical_profile_groups_on_owned_bridge

    member_diagnostic_state = {"used": False}
    member_diagnostic_lock = threading.Lock()

    def record_failed_member_delay(adapter: Any, logical_runtime_target: str, test_url: str) -> None:
        """Make at most one read-only loopback member delay request after a group failure."""
        with member_diagnostic_lock:
            if member_diagnostic_state["used"]:
                return
            member_diagnostic_state["used"] = True
        from urllib.parse import quote, urlsplit
        import re
        import httpx
        from .native_runner import _redact_diagnostic

        record: dict[str, Any] = {
            "schema": "fwrouter-acceptance-mihomo-member-delay-diagnostic/v1",
            "event": "attempt",
            "path_category": "proxy_delay",
        }
        try:
            controller = urlsplit(str(adapter.base_url))
            if (controller.scheme != "http" or controller.hostname != "127.0.0.1"
                    or controller.port != 5200 or controller.username or controller.password
                    or controller.path not in {"", "/"} or controller.query or controller.fragment):
                record["result"] = "controller_not_owned_loopback"
                return
            snapshot = adapter.get_logical_group_state(logical_runtime_target)
            members = snapshot.get("members") if isinstance(snapshot, dict) else None
            first = next((item for item in members or []
                          if isinstance(item, dict) and isinstance(item.get("runtime_identity"), str)
                          and item["runtime_identity"]), None)
            if first is None:
                record["result"] = "member_unavailable"
                record["member_available"] = False
                return
            record["member_available"] = True
            member = str(first["runtime_identity"])
            url = f"{adapter.base_url}/proxies/{quote(member, safe='')}/delay"
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                response = client.get(
                    url,
                    headers=adapter._headers(),
                    params={"timeout": 1000, "url": test_url},
                )
            record["status"] = int(response.status_code)
            body: Any = None
            try:
                body = json.loads(bytes(response.content[:8192]).decode("utf-8", "replace"))
            except (TypeError, ValueError):
                pass
            message = None
            code = None
            if isinstance(body, dict):
                message = body.get("message")
                error = body.get("error")
                if not isinstance(message, str):
                    message = error.get("message") if isinstance(error, dict) else error
                candidate_code = body.get("error_code")
                if isinstance(candidate_code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate_code):
                    code = candidate_code
            record["body_flags"] = {
                "json_object": isinstance(body, dict),
                "delay_present": isinstance(body, dict) and isinstance(body.get("delay"), int),
                "error_code_present": code is not None,
                "error_message_present": isinstance(message, str) and bool(message),
            }
            if code:
                record["error_code"] = code
            if isinstance(message, str) and message:
                safe = _redact_diagnostic(message, limit=512)
                safe = re.sub(r"(?i)\bhttps?://[^\s\"'<>]+", "[URL]", safe)
                safe = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b", "[ENDPOINT]", safe)
                safe = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[ADDRESS]", safe)
                safe = re.sub(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d{1,5})?\b", "[HOST]", safe)
                record["error_message"] = _redact_diagnostic(safe, limit=256)
            record["result"] = "response_received"
        except Exception as exc:
            record["result"] = "request_failed"
            record["exception_type"] = type(exc).__name__[:80]
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded) <= 1024:
                sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_MEMBER_DELAY " + encoded + "\n")
                sys.stderr.flush()
        except Exception:
            pass

    class LoopbackProbeMihomoHttpAdapter(MihomoHttpAdapter):
        def _acceptance_probe_url(self, test_url: str) -> str:
            return owned_probe_url

        def _probe_result_barrier(self, target: str, result: Any) -> None:
            reply = _rpc_call(socket_path, "mihomo_probe_result", {
                "logical_runtime_target": str(target), "probe_ok": bool(result.get("ok")) if isinstance(result, dict) else bool(getattr(result, "ok", False)),
            })
            if not reply.get("ok"):
                raise RuntimeError("acceptance native probe-result barrier failed")

        def probe_logical_group(self, logical_runtime_target: str, *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> dict[str, Any]:
            result = super().probe_logical_group(
                logical_runtime_target, test_url=self._acceptance_probe_url(test_url), timeout_ms=timeout_ms,
            )
            self._probe_result_barrier(logical_runtime_target, result)
            return result

        def probe_logical_member(self, logical_runtime_target: str, member_runtime_identity: str, *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> dict[str, Any]:
            result = super().probe_logical_member(
                logical_runtime_target, member_runtime_identity, test_url=self._acceptance_probe_url(test_url), timeout_ms=timeout_ms,
            )
            self._probe_result_barrier(logical_runtime_target, result)
            return result

        def probe_logical_groups(self, logical_runtime_targets: list[str], *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> list[dict[str, Any]]:
            result = super().probe_logical_groups(
                logical_runtime_targets, test_url=self._acceptance_probe_url(test_url), timeout_ms=timeout_ms,
            )
            failed = next((item for item in result if isinstance(item, dict) and item.get("ok") is False), None)
            if failed is not None and logical_runtime_targets:
                try:
                    record_failed_member_delay(
                        self, str(failed.get("logical_runtime_target") or logical_runtime_targets[0]),
                        self._acceptance_probe_url(test_url),
                    )
                except Exception:
                    pass
            for target, observed in zip(logical_runtime_targets, result):
                self._probe_result_barrier(target, observed)
            return result

        def check_delay(self, server_id: str, *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000):
            result = super().check_delay(server_id, test_url=self._acceptance_probe_url(test_url), timeout_ms=timeout_ms)
            self._probe_result_barrier(server_id, result)
            return result

    active_adapter = LoopbackProbeMihomoHttpAdapter(
        base_url="http://127.0.0.1:5200", config_path=active_config,
        contours_path=active_contours, timeout_seconds=3.0,
    )
    previous_adapter = adapter_module.DEFAULT_MIHOMO_ADAPTER
    adapter_module.DEFAULT_MIHOMO_ADAPTER = active_adapter
    for module in tuple(sys.modules.values()):
        if module is not None and getattr(module, "DEFAULT_MIHOMO_ADAPTER", None) is previous_adapter:
            setattr(module, "DEFAULT_MIHOMO_ADAPTER", active_adapter)

    def process_command(args: list[str], *, timeout_seconds: int = 30) -> dict[str, Any]:
        if args == ["ps", "-q", "mihomo"]:
            reply = _rpc_call(socket_path, "mihomo_incarnation", {})
            incarnation = str((reply.get("details") or {}).get("stdout") or "")
            return {"command": ["acceptance-mihomo", *args], "returncode": 0 if reply.get("ok") and incarnation else 1,
                    "stdout": incarnation.split("|", 1)[0] if incarnation else "", "stderr": "", "ok": bool(reply.get("ok") and incarnation)}
        if args == ["ps", "mihomo"]:
            reply = _rpc_call(socket_path, "mihomo_status", {})
            running = bool(reply.get("ok"))
            return {"command": ["acceptance-mihomo", *args], "returncode": 0 if running else 1,
                    "stdout": "mihomo running" if running else "mihomo stopped", "stderr": "", "ok": running}
        if args in (["restart", "mihomo"], ["up", "-d", "--force-recreate", "mihomo"]):
            reply = _rpc_call(socket_path, "mihomo_restart", {"config_path": str(active_config)})
            return {"command": ["acceptance-mihomo", *args], "returncode": 0 if reply.get("ok") else 1,
                    "stdout": str(reply.get("message") or ""), "stderr": str(reply.get("error_code") or ""), "ok": bool(reply.get("ok"))}
        raise RuntimeError("unsupported Mihomo process command in acceptance worker")

    def process_incarnation(*, timeout_seconds: float | None = None) -> str | None:
        reply = _rpc_call(socket_path, "mihomo_incarnation", {})
        value = str((reply.get("details") or {}).get("stdout") or "")
        return value if reply.get("ok") and "|" in value else None

    # These replace only the external Docker process transport. Config generation,
    # validation, controller HTTP calls, selection and reconciliation stay real.
    mihomo_runtime._run_compose_command = process_command
    mihomo_runtime.get_mihomo_runtime_incarnation = process_incarnation

    def validate_candidate(candidate_path: str | None = None, *, image_reference: str | None = None) -> dict[str, Any]:
        path = str(candidate_path or default_candidate)
        reply = _rpc_call(socket_path, "mihomo_test_config", {"path": path})
        details = reply.get("details") if isinstance(reply.get("details"), dict) else {}
        return {"ok": bool(reply.get("ok")), "returncode": 0 if reply.get("ok") else 1,
                "stdout_tail": str(details.get("stdout") or "")[-1000:],
                "stderr_tail": str(details.get("stderr") or "")[-1000:],
                "error_code": reply.get("error_code")}

    # Replace only the Docker validator transport with the pinned real Mihomo
    # binary's `-t`; candidate generation and validation outcome stay genuine.
    subscription_pipeline.validate_mihomo_candidate_config = validate_candidate
    return active_adapter


def _bind_acceptance_provider() -> None:
    """Point the genuine provider client at the owned loopback HTTP source."""
    endpoint = os.environ.get("FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL", "").strip()
    if not endpoint:
        return
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("acceptance provider transport is available only in the qualified test worker")
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("acceptance provider endpoint has an invalid port") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise RuntimeError("acceptance provider endpoint must be plain HTTP on loopback without a path")
    from fwrouter_api.adapters import stealthsurf

    base_client = stealthsurf.StealthSurfClient

    class LoopbackAcceptanceStealthSurfClient(base_client):
        def __init__(self, api_key: str, **kwargs: Any) -> None:
            kwargs["base_url"] = endpoint
            super().__init__(api_key, **kwargs)

    stealthsurf.StealthSurfClient = LoopbackAcceptanceStealthSurfClient



def main() -> None:
    _clean_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    socket_path = Path(args.socket)
    if str(socket_path) != os.environ.get("FWROUTER_ACCEPTANCE_RPC_SOCKET"):
        raise RuntimeError("worker RPC argument differs from the qualified environment")
    from .profile import load_profile
    profile, _ = load_profile(Path(os.environ.get("FWROUTER_ACCEPTANCE_PROFILE", "")))
    remove_handoff_alias = (
        _install_packet_handoff_loopback_alias(profile)
        if profile.get("profile") == "hosted-kernel-packet" else None
    )
    try:
        app = build_app(socket_path)
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    finally:
        if profile.get("profile") == "hosted-kernel-packet" and _PACKET_DNSMASQ_STATE:
            try:
                _packet_dnsmasq_fixture(Path(os.environ["FWROUTER_STATE_DIR"]), profile, stop=True)
            except Exception:
                sys.stderr.write("FWROUTER_ACCEPTANCE_DNS53_CLEANUP_FAILED\n")
                sys.stderr.flush()
        if remove_handoff_alias is not None:
            remove_handoff_alias()


if __name__ == "__main__":
    main()
