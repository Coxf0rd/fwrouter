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
import stat
import sys
import threading
import time
import traceback
from urllib.parse import quote, urlsplit
from pathlib import Path
from typing import Any


def _clean_environment() -> None:
    keep = {
        "PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
        "FWROUTER_STATE_DIR", "FWROUTER_ENVIRONMENT", "FWROUTER_STARTUP_TASKS_ENABLED",
        "FWROUTER_STARTUP_RECOVERY_ENABLED",
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
    os.environ.setdefault("FWROUTER_STARTUP_TASKS_ENABLED", "0")
    os.environ.setdefault("FWROUTER_STARTUP_RECOVERY_ENABLED", "0")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


_PACKET_DNSMASQ_LOCK = threading.RLock()
_PACKET_DNSMASQ_STATE: dict[str, Any] = {}


def _packet_dns_effective_artifact_observation() -> dict[str, Any]:
    """Capture non-atomic cache/path parity without changing the production DNS lookup."""
    try:
        from fwrouter_api.core.config import get_settings
        from fwrouter_api.services import dataplane_global, rules
        from fwrouter_api.services.live_probe_cache import _CACHE, _CACHE_LOCK, _INFLIGHT

        key = "dataplane_global.effective_rules_artifact"
        cached_entry_present = False
        cached_artifact_present: bool | None = None
        cached_value: Any = None
        cache_inflight_present = False
        with _CACHE_LOCK:
            cache_inflight_present = key in _INFLIGHT
            entry = _CACHE.get(key)
            if isinstance(entry, tuple) and len(entry) == 2 and type(entry[0]) in {int, float}:
                if entry[0] > time.monotonic():
                    cached_entry_present = True
                    cached_value = entry[1]
                    cached_artifact_present = isinstance(cached_value, dict)

        uncached_value = dataplane_global._read_effective_rules_artifact_uncached()
        try:
            # Only the writer path is needed. The text/export read-model also
            # decodes the full artifact and reads every rules text file, adding
            # diagnostic work inside the measured startup path.
            state = rules.get_rules_state()
            writer_path = Path(state["effective_json_path"]).resolve(strict=False)
            settings = get_settings()
            reader_paths = (
                (settings.paths.generated_dir / "rules" / "effective-rules.json").resolve(strict=False),
                (settings.paths.state_dir / "last-good" / "rules" / "effective-rules.json").resolve(strict=False),
            )
            writer_path_matches_reader = writer_path in reader_paths
            writer_path_exists = writer_path.is_file()
        except Exception:
            writer_path_matches_reader = None
            writer_path_exists = None

        return {
            "cache_entry_present": cached_entry_present,
            "cache_inflight_present": cache_inflight_present,
            "cached_artifact_present": cached_artifact_present,
            "uncached_artifact_present": isinstance(uncached_value, dict),
            "cached_matches_uncached": cached_value == uncached_value if cached_entry_present else None,
            "writer_path_matches_reader": writer_path_matches_reader,
            "writer_path_exists": writer_path_exists,
        }
    except Exception:
        return {
            "cache_entry_present": None,
            "cache_inflight_present": None,
            "cached_artifact_present": None,
            "uncached_artifact_present": None,
            "cached_matches_uncached": None,
            "writer_path_matches_reader": None,
            "writer_path_exists": None,
        }


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
            if module is not None and current.get("reconcile_dnsmasq_rules") is not None:
                module.reconcile_dnsmasq_rules = current["reconcile_dnsmasq_rules"]
            if module is not None and current.get("read_effective_rules_artifact") is not None:
                module.read_effective_rules_artifact = current["read_effective_rules_artifact"]
            settings = current.get("settings")
            if settings is not None:
                settings.lan_interface_allowlist = current.get("lan_allowlist", [])
            rescue_interface_cleaned = False
            if current.get("rescue0_owned") is True:
                import subprocess

                env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}

                def read_links() -> list[dict[str, Any]]:
                    inspected = subprocess.run(
                        ["/usr/sbin/ip", "-j", "link", "show"], check=False,
                        capture_output=True, timeout=3, stdin=subprocess.DEVNULL, env=env,
                    )
                    if (inspected.returncode != 0 or len(inspected.stdout) > 8192
                            or len(inspected.stderr) > 1024):
                        raise RuntimeError("bounded rescue0 teardown inspection failed")
                    try:
                        value = json.loads(inspected.stdout)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise RuntimeError("rescue0 teardown inspection was not valid JSON") from exc
                    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                        raise RuntimeError("rescue0 teardown inspection returned an invalid link list")
                    return value

                if any(item.get("ifname") == "rescue0" for item in read_links()):
                    deleted = subprocess.run(
                        ["/usr/sbin/ip", "link", "del", "dev", "rescue0"], check=False,
                        capture_output=True, timeout=3, stdin=subprocess.DEVNULL, env=env,
                    )
                    if (deleted.returncode != 0 or len(deleted.stdout) > 1024
                            or len(deleted.stderr) > 1024):
                        raise RuntimeError("bounded owned rescue0 teardown failed")
                if any(item.get("ifname") == "rescue0" for item in read_links()):
                    raise RuntimeError("owned rescue0 remained after teardown")
                rescue_interface_cleaned = True
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
                    "owned_paths_cleaned": cleaned,
                    "rescue_interface_cleaned": rescue_interface_cleaned}

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

        fixed_ip_env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}

        def inspect_links() -> list[dict[str, Any]]:
            inspected = subprocess.run(
                ["/usr/sbin/ip", "-j", "link", "show"], check=False,
                capture_output=True, timeout=3, stdin=subprocess.DEVNULL, env=fixed_ip_env,
            )
            if (inspected.returncode != 0 or len(inspected.stdout) > 8192
                    or len(inspected.stderr) > 1024):
                raise RuntimeError("bounded packet rescue0 link inspection failed")
            try:
                value = json.loads(inspected.stdout)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("packet rescue0 link inspection was not valid JSON") from exc
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise RuntimeError("packet rescue0 link inspection returned an invalid link list")
            return value

        if any(item.get("ifname") == "rescue0" for item in inspect_links()):
            raise RuntimeError("packet rescue0 already exists; refusing to adopt an unowned interface")
        # The qualified packet namespace is exclusively owned by this fixture.
        # Register teardown ownership before either bounded create command can fail.
        current["rescue0_owned"] = True
        rescue_link = subprocess.run(
            ["/usr/sbin/ip", "link", "add", "rescue0", "type", "dummy"],
            check=False, capture_output=True, timeout=3, stdin=subprocess.DEVNULL, env=fixed_ip_env,
        )
        if (rescue_link.returncode != 0 or len(rescue_link.stdout) > 1024
                or len(rescue_link.stderr) > 1024):
            raise RuntimeError("bounded packet rescue0 dummy creation failed")
        rescue_address = subprocess.run(
            ["/usr/sbin/ip", "address", "add", "10.254.254.1/32", "dev", "rescue0"],
            check=False, capture_output=True, timeout=3, stdin=subprocess.DEVNULL, env=fixed_ip_env,
        )
        if (rescue_address.returncode != 0 or len(rescue_address.stdout) > 1024
                or len(rescue_address.stderr) > 1024):
            raise RuntimeError("bounded packet rescue0 address creation failed")
        rescue_readback = subprocess.run(
            ["/usr/sbin/ip", "-j", "-4", "address", "show", "dev", "rescue0"],
            check=False, capture_output=True, timeout=3, stdin=subprocess.DEVNULL, env=fixed_ip_env,
        )
        if (rescue_readback.returncode != 0 or len(rescue_readback.stdout) > 4096
                or len(rescue_readback.stderr) > 1024):
            raise RuntimeError("bounded packet rescue0 address readback failed")
        try:
            rescue_rows = json.loads(rescue_readback.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("packet rescue0 address readback was not valid JSON") from exc
        rescue_bindings = []
        for interface in rescue_rows if isinstance(rescue_rows, list) else []:
            if not isinstance(interface, dict):
                continue
            for item in interface.get("addr_info") or []:
                if isinstance(item, dict):
                    rescue_bindings.append(
                        (interface.get("ifname"), item.get("local"), item.get("prefixlen"))
                    )
        if rescue_bindings != [("rescue0", "10.254.254.1", 32)]:
            raise RuntimeError("packet rescue0 address readback did not match its owned /32")

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
        original_reconcile_dnsmasq_rules = dnsmasq.reconcile_dnsmasq_rules
        original_read_effective_rules_artifact = dnsmasq.read_effective_rules_artifact

        def observed_read_effective_rules_artifact(*args: Any, **kwargs: Any) -> Any:
            observation = _packet_dns_effective_artifact_observation()
            try:
                from fwrouter_api.services.live_probe_cache import _CACHE, _CACHE_LOCK

                key = "dataplane_global.effective_rules_artifact"
                with _CACHE_LOCK:
                    entry = _CACHE.get(key)
                    observation["cache_entry_valid_at_reader"] = (
                        isinstance(entry, tuple) and len(entry) == 2
                        and type(entry[0]) in {int, float} and entry[0] > time.monotonic()
                    )
            except Exception:
                observation["cache_entry_valid_at_reader"] = None
            try:
                result = original_read_effective_rules_artifact(*args, **kwargs)
            except BaseException:
                observation["reader_returned_artifact"] = None
                observation["uncached_presence_matches_reader_result"] = None
                observation["cached_presence_matches_reader_result"] = None
                current["artifact_read_observation"] = observation
                raise
            reader_has_artifact = isinstance(result, dict)
            observation["reader_returned_artifact"] = reader_has_artifact
            observation["uncached_presence_matches_reader_result"] = isinstance(
                observation.get("uncached_artifact_present"), bool
            ) and observation.get("uncached_artifact_present") is reader_has_artifact
            cached_has_artifact = observation.get("cached_artifact_present")
            observation["cached_presence_matches_reader_result"] = (
                cached_has_artifact is reader_has_artifact
                if observation.get("cache_entry_present") is True else None
            )
            current["artifact_read_observation"] = observation
            return result

        def observed_reconcile_dnsmasq_rules(*args: Any, **kwargs: Any) -> Any:
            current.pop("artifact_read_observation", None)
            result = original_reconcile_dnsmasq_rules(*args, **kwargs)
            if isinstance(result, dict):
                error_code = result.get("error_code")
                if error_code not in {
                    "DNSMASQ_DNS_CAPTURE_FAILED", "DNSMASQ_RESTART_FAILED",
                    "DNSMASQ_SELECTIVE_CONTRACT_INCOMPLETE",
                }:
                    error_code = None
                restart_reason = result.get("restart_reason")
                if restart_reason not in {None, "config_changed", "nftset_probe_unhealthy"}:
                    restart_reason = None
                current["reconcile_result"] = {
                    "ok": result.get("ok") is True,
                    "error_code": error_code,
                    "restart_required": result.get("restart_required") is True,
                    "restart_reason": restart_reason,
                    "selective_status_ok": (result.get("selective_status") or {}).get("ok") is True
                    if isinstance(result.get("selective_status"), dict) else None,
                    "artifact_read": current.get("artifact_read_observation"),
                }
            return result

        current.update({"module": dnsmasq, "subprocess_module": dnsmasq.subprocess,
                        "reconcile_dnsmasq_rules": original_reconcile_dnsmasq_rules,
                        "read_effective_rules_artifact": original_read_effective_rules_artifact,
                        "settings": settings, "lan_allowlist": old_allowlist, "paths": old_paths})
        dnsmasq.read_effective_rules_artifact = observed_read_effective_rules_artifact
        dnsmasq.reconcile_dnsmasq_rules = observed_reconcile_dnsmasq_rules
        settings.lan_interface_allowlist = [lan_rows[0][0], "rescue0"]
        discovered_lan_bindings = dnsmasq._discover_router_dns_bindings()
        if discovered_lan_bindings != [{"ifname": lan_rows[0][0], "address": "10.240.0.1"}]:
            raise RuntimeError("packet DNS discovery did not exclude the allowlisted rescue0 role")
        current["rescue_discovery_receipt"] = {
            "rescue0_allowlisted": "rescue0" in settings.lan_interface_allowlist,
            "rescue0_role_excluded": all(item.get("ifname") != "rescue0" for item in discovered_lan_bindings),
            "router_dns_interfaces": sorted({item["ifname"] for item in discovered_lan_bindings}),
        }
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
            try:
                test_result = subprocess.run([*arguments, "--test"], check=False, capture_output=True,
                                             timeout=5, stdin=subprocess.DEVNULL,
                                             env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C", "TZ": "UTC"})
            except subprocess.TimeoutExpired:
                runtime.update({"config_test_status": "timeout", "config_test_returncode": None,
                                "config_test_error_code": "config_test_timeout"})
                raise subprocess.CalledProcessError(1, [*arguments, "--test"], output=b"",
                                                    stderr=b"native dnsmasq config test timed out")
            stdout_size = len(test_result.stdout or b"")
            stderr_size = len(test_result.stderr or b"")
            status = "passed" if test_result.returncode == 0 and stdout_size <= 2048 and stderr_size <= 2048 else "failed"
            runtime.update({"config_test_status": status,
                            "config_test_returncode": test_result.returncode
                            if type(test_result.returncode) is int and -255 <= test_result.returncode <= 255 else None,
                            "config_test_stdout_bytes": stdout_size if stdout_size <= 2048 else 2049,
                            "config_test_stderr_bytes": stderr_size if stderr_size <= 2048 else 2049,
                            "config_test_error_code": None if status == "passed" else
                            "config_test_output_bound_exceeded" if stdout_size > 2048 or stderr_size > 2048 else
                            "config_test_rejected"})
            if status != "passed":
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


def _install_acceptance_live_mode_probe_observer() -> None:
    """Log bounded actual global-mode probe results without altering the probe."""
    from fwrouter_api.services import apply as apply_service

    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("live mode probe observer requires the qualified acceptance worker")
    original_probe = apply_service.probe_live_global_mode
    state = {"count": 0}
    allowed_modes = {"direct", "selective", "vpn", "core_bypass", "unknown"}
    allowed_defaults = {"direct", "vpn"}
    allowed_errors = {"NFT_NOT_AVAILABLE", "NFT_CHAIN_READ_FAILED"}

    def observed_probe() -> dict[str, Any]:
        result = original_probe()
        if state["count"] < 64:
            state["count"] += 1
            mode = result.get("mode") if isinstance(result, dict) else None
            selective_default = result.get("selective_default") if isinstance(result, dict) else None
            error_code = result.get("error_code") if isinstance(result, dict) else None
            raw_chain = result.get("raw_chain") if isinstance(result, dict) else None
            raw_chain = raw_chain if isinstance(raw_chain, str) else ""
            receipt = {
                "schema": "fwrouter-acceptance-live-mode-probe/v1",
                "sequence": state["count"],
                "ok": result.get("ok") if isinstance(result, dict) and type(result.get("ok")) is bool else None,
                "mode": mode if isinstance(mode, str) and mode in allowed_modes else "other",
                "selective_default": (selective_default if isinstance(selective_default, str)
                                       and selective_default in allowed_defaults else None),
                "error_code": (error_code if isinstance(error_code, str) and error_code in allowed_errors
                               else "other" if isinstance(error_code, str) and error_code else None),
                "comment_categories": {
                    "classify_chain_present": 'chain fwrouter_classify {' in raw_chain,
                    "global_direct": 'goto fwrouter_direct comment "global direct v1"' in raw_chain,
                    "global_vpn": 'goto fwrouter_vpn_full comment "global vpn v1"' in raw_chain,
                    "global_selective_default_vpn": 'comment "selective default vpn"' in raw_chain,
                    "global_selective_default_direct": 'comment "selective default direct"' in raw_chain,
                    "global_selective_degraded_default_direct": (
                        'comment "selective degraded default direct"' in raw_chain),
                    "scoped_selective_rules": 'comment "scoped selective ' in raw_chain,
                    "scoped_selective_default_vpn": 'comment "scoped selective default vpn:' in raw_chain,
                    "scoped_selective_default_direct": 'comment "scoped selective default direct:' in raw_chain,
                    "scoped_selective_degraded_default_direct": (
                        'comment "scoped selective degraded default direct:' in raw_chain),
                },
            }
            print("FWROUTER_ACCEPTANCE_LIVE_MODE_PROBE " + json.dumps(
                receipt, sort_keys=True, separators=(",", ":")), flush=True)
        return result

    apply_service.probe_live_global_mode = observed_probe


def _install_acceptance_preflight_observer() -> None:
    """Log fixed readiness inputs and outputs at manifest and apply preflight boundaries."""
    from fwrouter_api.services import apply as apply_service, routing_manifest as manifest_service

    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("preflight observer requires the qualified acceptance worker")
    allowed_modes = {"direct", "selective", "vpn"}
    allowed_missing = {
        "domain_selective_enforcement_not_implemented",
        "mihomo_tproxy_port_not_configured", "mihomo_tun_not_enabled",
        "mihomo_global_egress_binding_unknown", "mihomo_controller_unreachable",
        "mihomo_transparent_contour_not_ready", "mihomo_explicit_proxy_isolation_not_ready",
        "dnsmasq_domain_selective_contract_not_ready", "effective_rules_artifact_missing",
        "fwrouter_owned_table_required_chains_missing", "vpn_module_disabled",
        "vpn_tproxy_contract_not_defined", "vpn_external_path_not_verified",
        "active_dataplane_mode_mismatch", "vpn_tproxy_handoff_not_observed",
        "vpn_udp_fail_closed_policy_unconfirmed",
    }
    state = {"count": 0}

    def observe(service: Any, origin: str) -> None:
        original_preflight = service.build_global_preflight

        def observed_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
            original_result = original_preflight(*args, **kwargs)
            if state["count"] < 64:
                state["count"] += 1
                routing = kwargs.get("routing")
                routing = routing if isinstance(routing, dict) else {}
                requested_mode = routing.get("applied_mode") or routing.get("desired_mode")
                requested_mode = requested_mode if isinstance(requested_mode, str) and requested_mode in allowed_modes else "other"
                check_details = kwargs.get("check_details")
                check_details = check_details if isinstance(check_details, dict) else {}
                projected_result = original_result if isinstance(original_result, dict) else {}
                selective_rules = projected_result.get("selective_rules")
                selective_rules = selective_rules if isinstance(selective_rules, dict) else {}
                missing_by_mode = projected_result.get("missing_by_mode")
                missing_by_mode = missing_by_mode if isinstance(missing_by_mode, dict) else {}

                def safe_missing(value: Any) -> tuple[list[str], int]:
                    if not isinstance(value, list):
                        return [], 0
                    allowed = {item for item in value if isinstance(item, str) and item in allowed_missing}
                    unknown_count = sum(isinstance(item, str) and item not in allowed_missing for item in value)
                    return sorted(allowed)[:16], min(unknown_count, 1_000_000)

                selective_degraded_missing, selective_degraded_unknown = safe_missing(
                    projected_result.get("selective_degraded_missing"))
                selective_missing, selective_unknown = safe_missing(missing_by_mode.get("selective"))
                vpn_missing, vpn_unknown = safe_missing(missing_by_mode.get("vpn"))

                def safe_bool(value: Any) -> bool | None:
                    return value if type(value) is bool else None

                receipt = {
                    "schema": "fwrouter-acceptance-preflight/v1",
                    "sequence": state["count"],
                    "origin": origin,
                    "routing_mode": requested_mode,
                    "require_runtime_verify": safe_bool(kwargs.get("require_runtime_verify")),
                    "selective_requires_vpn_runtime": safe_bool(selective_rules.get("requires_vpn_runtime")),
                    "selective_vpn_ready": safe_bool(projected_result.get("selective_vpn_ready")),
                    "selective_vpn_policy_ready": safe_bool(
                        projected_result.get("selective_vpn_policy_ready")),
                    "selective_degraded": safe_bool(projected_result.get("selective_degraded")),
                    "selective_degraded_missing": selective_degraded_missing,
                    "selective_degraded_unknown_missing_count": selective_degraded_unknown,
                    "selective_missing": selective_missing,
                    "selective_unknown_missing_count": selective_unknown,
                    "vpn_missing": vpn_missing,
                    "vpn_unknown_missing_count": vpn_unknown,
                    "check_external_path_verified": safe_bool(check_details.get("vpn_external_path_verified")),
                    "preflight_external_path_verified": safe_bool(projected_result.get("vpn_external_path_verified")),
                    "vpn_policy_installed": safe_bool(projected_result.get("vpn_policy_installed")),
                    "vpn_egress_target_present": safe_bool(projected_result.get("vpn_egress_target_present")),
                    "vpn_egress_ready": safe_bool(projected_result.get("vpn_egress_ready")),
                }
                print("FWROUTER_ACCEPTANCE_PREFLIGHT " + json.dumps(
                    receipt, sort_keys=True, separators=(",", ":")), flush=True)
            return original_result

        service.build_global_preflight = observed_preflight

    observe(manifest_service, "manifest")
    observe(apply_service, "apply")


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
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    from tests.application_acceptance.diagnostics import encode_safe_reentry_native_record

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
    original_mihomo_health = MihomoHttpAdapter.health
    original_mihomo_proxies = MihomoHttpAdapter._proxies
    original_active_config_hash = mihomo_reconcile_fingerprint.active_config_hash
    original_runtime_incarnation = mihomo_reconcile_fingerprint._current_runtime_incarnation
    original_load_reconcile_state = mihomo_reconcile_fingerprint._load_state

    def safe_digest(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()

    def safe_sample(proxy: Any, owned_probe_url: str | None) -> dict[str, Any]:
        if not isinstance(proxy, dict) or not owned_probe_url:
            return {"present": False, "alive": None, "latest_delay_ms": None,
                    "history_age_ms": None, "history_fresh_5s": False}
        extra = proxy.get("extra") if isinstance(proxy.get("extra"), dict) else {}
        sample = extra.get(owned_probe_url)
        if not isinstance(sample, dict):
            return {"present": False, "alive": None, "latest_delay_ms": None,
                    "history_age_ms": None, "history_fresh_5s": False}
        history = sample.get("history") if isinstance(sample.get("history"), list) else []
        latest = history[-1] if history and isinstance(history[-1], dict) else {}
        delay = latest.get("delay")
        delay_ms = delay if type(delay) is int and 0 <= delay <= 65535 else None
        age_ms = None
        raw_time = latest.get("time")
        if isinstance(raw_time, str) and len(raw_time) <= 64:
            try:
                from datetime import datetime, timezone
                parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    age = int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() * 1000)
                    if abs(age) <= 31_536_000_000:
                        age_ms = age
            except (OverflowError, TypeError, ValueError):
                pass
        return {
            "present": True,
            "alive": sample.get("alive") if type(sample.get("alive")) is bool else None,
            "latest_delay_ms": delay_ms,
            "history_age_ms": age_ms,
            "history_fresh_5s": type(age_ms) is int and 0 <= age_ms <= 5000,
        }

    def capture_native_reentry_snapshot(adapter: Any, health: Any, proxies: Any) -> None:
        """Keep a short ring of native proof snapshots, including final verification."""
        if getattr(local, "operation", None) != 3 or getattr(local, "phase", None) != "core_apply":
            return
        snapshots = getattr(local, "native_snapshots", None)
        if not isinstance(snapshots, list):
            snapshots = []
            local.native_snapshots = snapshots
        sequence = getattr(local, "native_sequence", 0) + 1
        local.native_sequence = sequence
        details = health.details if isinstance(getattr(health, "details", None), dict) else {}
        policy = details.get("vpn_udp_policy") if isinstance(details.get("vpn_udp_policy"), dict) else {}
        selectors = details.get("selectors") if isinstance(details.get("selectors"), dict) else {}
        runtime_state = getattr(health, "runtime_state", "")
        runtime_state = getattr(runtime_state, "value", runtime_state)
        by_name = proxies if isinstance(proxies, dict) else {}
        vpn_auto = by_name.get("vpn-auto") if isinstance(by_name.get("vpn-auto"), dict) else {}
        vpn_global = by_name.get("vpn-global") if isinstance(by_name.get("vpn-global"), dict) else {}
        selected_auto = vpn_auto.get("now") if isinstance(vpn_auto.get("now"), str) else None
        selected_global = vpn_global.get("now") if isinstance(vpn_global.get("now"), str) else None
        target_proxy = by_name.get(selected_auto) if selected_auto else None
        if not isinstance(target_proxy, dict):
            target_proxy = vpn_auto
        probe_url = os.environ.get("FWROUTER_ACCEPTANCE_MIHOMO_PROBE_URL")
        config_sha = safe_digest(getattr(local, "reentry_active_config_hash", None))
        runtime_sha = safe_digest(getattr(local, "reentry_runtime_incarnation", None))
        revision = None
        try:
            from fwrouter_api.db.connection import db_session
            from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
            with db_session() as connection:
                revision = read_selection_revision(connection)
        except Exception:
            pass
        policy_keys = (
            "policy_installed", "wiring_proven", "native_top_level_rules_match",
            "selector_membership_matches", "guarded_rules_match", "reconcile_receipt_matches",
        )
        receipt = getattr(local, "reentry_receipt", None)
        snapshot = {
            "schema": "fwrouter-acceptance-reentry-native-proof/v1",
            "operation": 3,
            "sequence": sequence,
            "phase": "core_apply",
            "runtime_running": str(runtime_state) == "running",
            "proof": {key: policy[key] for key in policy_keys if type(policy.get(key)) is bool},
            "proof_fields_present": sorted(key for key in policy_keys if key in policy),
            "selector_readback": {
                "vpn_auto_exists": selectors.get("vpn_auto_exists") is True,
                "vpn_global_exists": selectors.get("vpn_global_exists") is True,
                "vpn_global_has_vpn_auto": selectors.get("vpn_global_has_vpn_auto") is True,
                "vpn_global_now_is_vpn_auto": selectors.get("vpn_global_now") == "vpn-auto",
                "vpn_auto_selected_sha256": safe_digest(selected_auto),
                "vpn_global_selected_sha256": safe_digest(selected_global),
                "vpn_auto_targets_count": min(selectors.get("vpn_auto_targets_count", 0), 10000)
                if type(selectors.get("vpn_auto_targets_count")) is int else 0,
                "vpn_auto_now_in_targets": (
                    selectors.get("vpn_auto_now") in selectors.get("vpn_auto_targets", [])
                    if isinstance(selectors.get("vpn_auto_targets"), list) else False
                ),
            },
            "native_selected_probe_history": safe_sample(target_proxy, probe_url),
            "active_config_sha256": config_sha,
            "runtime_incarnation_sha256": runtime_sha,
            "receipt": receipt,
            "selection_revision": revision if type(revision) is int and 0 <= revision <= 2**63 - 1 else None,
        }
        snapshots.append(snapshot)
        del snapshots[:-4]

    def flush_native_reentry_snapshots() -> None:
        snapshots = getattr(local, "native_snapshots", [])
        for snapshot in snapshots[-4:]:
            encoded = encode_safe_reentry_native_record(snapshot, max_bytes=3500)
            if encoded is not None:
                sys.stderr.write("FWROUTER_ACCEPTANCE_REENTRY_NATIVE " + encoded + "\n")
        if snapshots:
            sys.stderr.flush()

    def observed_mihomo_proxies(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_mihomo_proxies(self, *args, **kwargs)
        if getattr(local, "operation", None) == 3 and getattr(local, "phase", None) == "core_apply":
            local.native_proxies = result
        return result

    def observed_mihomo_health(self: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(local, "operation", None) == 3 and getattr(local, "phase", None) == "core_apply":
            local.native_proxies = {}
            local.reentry_active_config_hash = None
            local.reentry_runtime_incarnation = None
            local.reentry_receipt = None
            local.reentry_receipt_raw = {}
            local.native_snapshots = []
            local.native_sequence = 0
        result = original_mihomo_health(self, *args, **kwargs)
        if getattr(local, "operation", None) == 3 and getattr(local, "phase", None) == "core_apply":
            capture_native_reentry_snapshot(self, result, getattr(local, "native_proxies", {}))
        return result

    def observed_active_config_hash() -> str | None:
        value = original_active_config_hash()
        if getattr(local, "operation", None) == 3 and getattr(local, "phase", None) == "core_apply":
            local.reentry_active_config_hash = value
            receipt = getattr(local, "reentry_receipt", None)
            if isinstance(receipt, dict):
                receipt["active_config_matches"] = bool(
                    value and getattr(local, "reentry_receipt_raw", {}).get("active_config_hash") == value
                )
        return value

    def observed_runtime_incarnation() -> str | None:
        value = original_runtime_incarnation()
        if getattr(local, "operation", None) == 3 and getattr(local, "phase", None) == "core_apply":
            local.reentry_runtime_incarnation = value
            receipt = getattr(local, "reentry_receipt", None)
            if isinstance(receipt, dict):
                receipt["runtime_incarnation_matches"] = bool(
                    value and getattr(local, "reentry_receipt_raw", {}).get("runtime_incarnation") == value
                )
        return value

    def observed_reconcile_state() -> dict[str, Any] | None:
        loaded = original_load_reconcile_state()
        if getattr(local, "operation", None) == 3 and getattr(local, "phase", None) == "core_apply":
            local.reentry_receipt_raw = loaded if isinstance(loaded, dict) else {}
            local.reentry_receipt = {
                "present": isinstance(loaded, dict),
                "active_config_matches": False,
                "runtime_incarnation_matches": False,
                "udp_policy_version_matches": (
                    loaded.get("udp_policy_version") == mihomo_reconcile_fingerprint.UDP_POLICY_RECEIPT_VERSION
                    if isinstance(loaded, dict) else False
                ),
                "native_top_level_rules_match": loaded.get("native_top_level_rules_match") is True
                if isinstance(loaded, dict) else False,
                "native_selector_membership_match": loaded.get("native_selector_membership_match") is True
                if isinstance(loaded, dict) else False,
                "subrule_config_guard_match": loaded.get("subrule_config_guard_match") is True
                if isinstance(loaded, dict) else False,
            }
        return loaded

    MihomoHttpAdapter._proxies = observed_mihomo_proxies
    MihomoHttpAdapter.health = observed_mihomo_health
    mihomo_reconcile_fingerprint.active_config_hash = observed_active_config_hash
    mihomo_reconcile_fingerprint._current_runtime_incarnation = observed_runtime_incarnation
    mihomo_reconcile_fingerprint._load_state = observed_reconcile_state

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
        previous_phase = getattr(local, "phase", None)
        local.phase = "core_apply"
        try:
            result = original_core_apply(*args, **kwargs)
        except Exception as exc:
            record("core_apply_exception", operation=operation,
                   exception_type=type(exc).__name__)
            raise
        finally:
            flush_native_reentry_snapshots()
            local.phase = previous_phase
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


def _install_acceptance_mihomo_delay_error_observer() -> dict[str, Any]:
    """Capture bounded Mihomo delay error bodies without changing HTTP behavior."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("Mihomo delay observer requires the qualified acceptance worker")

    import re
    import httpx
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from .native_runner import _redact_diagnostic

    original_raise = httpx.Response.raise_for_status
    client_type = httpx.Client
    original_client_get = client_type.get
    original_zero_readback = MihomoHttpAdapter._has_fresh_zero_delay_for_url
    state = {"records": 0, "success_samples": 0, "member_state_snapshot_used": False,
             "zero_readback_records": 0, "group_probe_events": []}
    lock = threading.Lock()
    group_probe_context = threading.local()
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

    def begin_probe(kind: str, runtime_target: str, owned_test_url: str) -> dict[str, Any]:
        previous = getattr(group_probe_context, "current", None)
        token = {"kind": kind, "runtime_target": runtime_target,
                 "owned_test_url": owned_test_url, "event": None, "previous": previous}
        group_probe_context.current = token
        return token

    def finish_group_probe(token: dict[str, Any], result: Any) -> dict[str, Any]:
        if getattr(group_probe_context, "current", None) is token:
            previous = token.get("previous")
            if previous is None:
                del group_probe_context.current
            else:
                group_probe_context.current = previous
        event = token.get("event")
        if not isinstance(event, dict):
            event = {
                "request_observed": False,
                "request_kind": token.get("kind"),
                "response_received": False,
                "status": None,
                "url_matches_known_owned": False,
                "payload_map_empty": None,
                "integer_delay_result_found": False,
                "integer_delay_zero": None,
                "error_code": "no_matching_delay_request",
                "request_started_monotonic_ms": None,
                "request_finished_monotonic_ms": None,
                "request_elapsed_ms": None,
            }
        event["adapter_probe_ok"] = bool(result.get("ok")) if isinstance(result, dict) else False
        with lock:
            events = state["group_probe_events"]
            events.append(dict(event))
            del events[:-8]
        return dict(event)

    def request_probe_path_matches(url: Any, token: dict[str, Any]) -> bool:
        try:
            parsed = httpx.URL(str(url))
            if token.get("kind") == "group":
                expected_path = "/group/" + quote(str(token["runtime_target"]), safe="") + "/delay"
            elif token.get("kind") == "member":
                expected_path = "/proxies/" + quote(str(token["runtime_target"]), safe="") + "/delay"
            else:
                return False
            return (parsed.scheme == "http" and parsed.host == "127.0.0.1" and parsed.port == 5200
                    and parsed.raw_path.split(b"?", 1)[0] == expected_path.encode("ascii"))
        except (TypeError, ValueError):
            return False

    def canonical_http_error(exc: BaseException) -> str:
        for error_type, code in (
            (httpx.ConnectTimeout, "connect_timeout"),
            (httpx.ReadTimeout, "read_timeout"),
            (httpx.WriteTimeout, "write_timeout"),
            (httpx.PoolTimeout, "pool_timeout"),
            (httpx.ConnectError, "connect_error"),
            (httpx.ReadError, "read_error"),
            (httpx.WriteError, "write_error"),
            (httpx.RemoteProtocolError, "remote_protocol_error"),
            (httpx.LocalProtocolError, "local_protocol_error"),
            (httpx.NetworkError, "network_error"),
            (httpx.HTTPError, "http_error"),
            (OSError, "os_error"),
        ):
            if isinstance(exc, error_type):
                return code
        return "other_error"

    def bounded_request_timing(started_ms: int, finished_ms: int) -> dict[str, int | None]:
        elapsed_ms = finished_ms - started_ms
        if (0 <= started_ms <= 9_007_199_254_740_991
                and 0 <= finished_ms <= 9_007_199_254_740_991
                and 0 <= elapsed_ms <= 60_000):
            return {
                "request_started_monotonic_ms": started_ms,
                "request_finished_monotonic_ms": finished_ms,
                "request_elapsed_ms": elapsed_ms,
            }
        return {
            "request_started_monotonic_ms": None,
            "request_finished_monotonic_ms": None,
            "request_elapsed_ms": None,
        }

    def observed_group_client_get(client: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        token = getattr(group_probe_context, "current", None)
        if not isinstance(token, dict) or not request_probe_path_matches(url, token):
            return original_client_get(client, url, *args, **kwargs)
        params = kwargs.get("params")
        try:
            supplied_test_url = params.get("url") if params is not None else None
        except (AttributeError, TypeError):
            supplied_test_url = None
        url_matches = supplied_test_url == token.get("owned_test_url")
        request_started_monotonic_ms = time.monotonic_ns() // 1_000_000
        try:
            response = original_client_get(client, url, *args, **kwargs)
        except Exception as exc:
            request_finished_monotonic_ms = time.monotonic_ns() // 1_000_000
            token["event"] = {
                "request_observed": True,
                "request_kind": token.get("kind"),
                "response_received": False,
                "status": None,
                "url_matches_known_owned": url_matches,
                "payload_map_empty": None,
                "integer_delay_result_found": False,
                "integer_delay_zero": None,
                "error_code": canonical_http_error(exc),
                **bounded_request_timing(request_started_monotonic_ms, request_finished_monotonic_ms),
            }
            raise
        request_finished_monotonic_ms = time.monotonic_ns() // 1_000_000
        payload: Any = None
        try:
            if len(response.content) <= 8192:
                payload = response.json()
        except (TypeError, ValueError):
            pass
        payload_is_map = isinstance(payload, dict)
        delay_values = ([value for value in payload.values()
                         if type(value) is int and 0 <= value <= 65535][:32]
                        if payload_is_map else [])
        status = int(response.status_code)
        message = payload.get("message") if payload_is_map else None
        error_code = None
        if status == 503 and message == "An error occurred in the delay test":
            error_code = "mihomo_generic_delay_failure"
        elif status == 504 and message == "Timeout":
            error_code = "mihomo_timeout"
        elif status >= 400:
            error_code = "other_http_error"
        token["event"] = {
            "request_observed": True,
            "request_kind": token.get("kind"),
            "response_received": True,
            "status": status if 100 <= status <= 599 else None,
            "url_matches_known_owned": url_matches,
            "payload_map_empty": not payload if payload_is_map else None,
            "integer_delay_result_found": bool(delay_values),
            "integer_delay_zero": (any(value == 0 for value in delay_values)
                                   if delay_values else None),
            "error_code": error_code,
            **bounded_request_timing(request_started_monotonic_ms, request_finished_monotonic_ms),
        }
        return response

    client_type.get = observed_group_client_get

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
    return {"begin_probe": begin_probe, "finish_group_probe": finish_group_probe}


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
    if os.environ.get("FWROUTER_STARTUP_RECOVERY_ENABLED") not in {"0", "1"}:
        raise RuntimeError("invalid acceptance startup recovery flag")
    if os.environ.get("FWROUTER_STARTUP_RECOVERY_ENABLED") == "1" and (
        acceptance_profile.get("profile") != "hosted-kernel-packet"
        or os.environ.get("FWROUTER_STARTUP_TASKS_ENABLED") != "1"
    ):
        raise RuntimeError("startup recovery is not authorized outside qualified kernel worker")

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
    from fwrouter_api import main as application_main
    bootstrap_observation: dict[str, Any] = {
        "schema": "fwrouter-runtime-bootstrap-observation/v1", "worker_pid": os.getpid(),
        "recovery_enabled": False, "foundation_ok": False, "completed": False,
    }
    original_bootstrap = application_main.bootstrap_backend
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    from fwrouter_api.services import bootstrap as bootstrap_service
    from fwrouter_api.services import mihomo_config, subscription_pipeline
    from fwrouter_api.services import apply_orchestrator, mihomo_reconcile
    from fwrouter_api.services import xray_subscription_service

    bootstrap_diagnostic_lock = threading.Lock()
    bootstrap_diagnostic_active = False
    bootstrap_diagnostic_counts: dict[str, int] = {}
    bootstrap_diagnostic_limit = 8

    def candidate_fingerprint(candidate_value: Any) -> dict[str, Any] | None:
        if not isinstance(candidate_value, (str, Path)):
            return None
        candidate_path = Path(candidate_value)
        try:
            info = candidate_path.lstat()
            resolved = candidate_path.resolve(strict=True)
            resolved.relative_to(state.resolve(strict=True))
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()):
                return None
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(resolved, flags)
            with os.fdopen(descriptor, "rb") as stream:
                opened_info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened_info.st_mode) or opened_info.st_uid != os.getuid()
                        or opened_info.st_size > 64 * 1024 * 1024):
                    return None
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return {
                "candidate": resolved.name,
                "bytes": opened_info.st_size,
                "sha256": digest.hexdigest(),
            }
        except (OSError, RuntimeError, ValueError):
            return None

    def observe_xray_generation_phase(name: str, original: Any) -> Any:
        def observed(*args: Any, **kwargs: Any) -> Any:
            nonlocal bootstrap_diagnostic_active
            if not bootstrap_diagnostic_active:
                return original(*args, **kwargs)
            with bootstrap_diagnostic_lock:
                count = min(bootstrap_diagnostic_counts.get(name, 0) + 1, 10_000)
                bootstrap_diagnostic_counts[name] = count
                bootstrap_observation["phase_call_counts"] = dict(bootstrap_diagnostic_counts)
                record_phase = count <= bootstrap_diagnostic_limit
            if not record_phase:
                return original(*args, **kwargs)
            result: Any = None
            candidate_validation = name in {
                "mihomo_config.validate_candidate", "subscription_pipeline.validate_candidate",
            }
            candidate_value = kwargs.get("candidate_path")
            if candidate_validation and candidate_value is None and name == "subscription_pipeline.validate_candidate":
                candidate_value = args[0] if args else None
            fingerprint_wall_before = time.monotonic()
            fingerprint_cpu_before = time.process_time()
            candidate_before = candidate_fingerprint(candidate_value) if candidate_validation else None
            fingerprint_before_wall = time.monotonic() - fingerprint_wall_before
            fingerprint_before_cpu = time.process_time() - fingerprint_cpu_before
            started = time.monotonic()
            cpu_started = time.process_time()
            prefix = "FWROUTER_ACCEPTANCE_BOOTSTRAP_PHASE "
            print(prefix + json.dumps({"phase": name, "event": "enter", "call": count}), flush=True)
            try:
                result = original(*args, **kwargs)
                if name == "_prospective_profile_bindings":
                    from .xray_support import binding_identity_summary
                    from fwrouter_api.db.connection import db_session
                    config_path = state / "xray" / "config.json"
                    if config_path.stat().st_size > 4 * 1024 * 1024:
                        raise RuntimeError("qualified diagnostic config exceeds size bound")
                    config = json.loads(config_path.read_text())
                    inbound = next(item for item in config["inbounds"] if item.get("tag") == "vless-ws")
                    expected = {(str(item["id"]), str(item["email"]))
                                for item in inbound["settings"]["clients"]}
                    with db_session() as connection:
                        def origin_lookup(subject_id):
                            row = connection.execute(
                                "SELECT subject_type,subject_role,is_active,is_deleted FROM subjects WHERE subject_id=?",
                                (subject_id,),
                            ).fetchone()
                            return dict(row) if row is not None else {}
                        summary = binding_identity_summary(result[0], expected, origin_lookup)
                    bootstrap_observation["prospective_binding_identity_parity"] = summary
                    print("FWROUTER_ACCEPTANCE_BINDING_IDENTITY_PARITY " + json.dumps(summary), flush=True)
                elif name == "_stage_profile_native_candidates" and isinstance(result, dict):
                    native = result.get("native_validation")
                    safe: dict[str, Any] = {"ok": result.get("ok") is True}
                    if isinstance(native, dict):
                        for phase in ("transition", "final"):
                            candidate = native.get(phase)
                            if not isinstance(candidate, dict):
                                continue
                            candidate_digest = candidate.get("candidate_sha256")
                            validation = candidate.get("validation")
                            if not isinstance(candidate_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", candidate_digest):
                                continue
                            validation = validation if isinstance(validation, dict) else {}
                            safe[phase] = {
                                "candidate_sha256": candidate_digest,
                                "validation_ok": validation.get("ok") is True,
                                "candidate_write_reused": validation.get("candidate_write_reused") is True,
                                "native_validation_reused": validation.get("native_validation_reused") is True,
                            }
                        transition = safe.get("transition")
                        final = safe.get("final")
                        if isinstance(transition, dict) and isinstance(final, dict):
                            safe["candidate_hashes_match"] = (
                                transition["candidate_sha256"] == final["candidate_sha256"]
                            )
                    bootstrap_observation["staged_native_candidate_diagnostics"] = safe
                    print("FWROUTER_ACCEPTANCE_STAGED_NATIVE_CANDIDATES " + json.dumps(safe), flush=True)
                return result
            finally:
                elapsed = round(time.monotonic() - started, 4)
                cpu_elapsed = round(time.process_time() - cpu_started, 4)
                with bootstrap_diagnostic_lock:
                    timings = bootstrap_observation.setdefault("phase_timings", {})
                    timings.setdefault(name, []).append(elapsed)
                    cpu_timings = bootstrap_observation.setdefault("phase_cpu_timings", {})
                    cpu_timings.setdefault(name, []).append(cpu_elapsed)
                if candidate_validation:
                    fingerprint_after_wall = time.monotonic()
                    fingerprint_after_cpu = time.process_time()
                    candidate_after = candidate_fingerprint(candidate_value)
                    fingerprint_after_cpu = time.process_time() - fingerprint_after_cpu
                    fingerprint_after_wall = time.monotonic() - fingerprint_after_wall
                    validation_summary: dict[str, Any] = {
                        "phase": name,
                        "role": "native" if name == "subscription_pipeline.validate_candidate" else "local",
                        "elapsed_seconds": elapsed,
                        "cpu_seconds": cpu_elapsed,
                        "diagnostic_fingerprint_seconds": round(
                            fingerprint_before_wall + fingerprint_after_wall, 4,
                        ),
                        "diagnostic_fingerprint_cpu_seconds": round(
                            fingerprint_before_cpu + fingerprint_after_cpu, 4,
                        ),
                        "candidate_before": candidate_before,
                        "candidate_after": candidate_after,
                    }
                    if isinstance(result, dict):
                        if type(result.get("ok")) is bool:
                            validation_summary["ok"] = result["ok"]
                        if type(result.get("returncode")) is int and -1 <= result["returncode"] <= 255:
                            validation_summary["returncode"] = result["returncode"]
                        error_code = result.get("error_code")
                        if isinstance(error_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code):
                            validation_summary["error_code"] = error_code
                    elif result is None:
                        validation_summary["result"] = "raised"
                    with bootstrap_diagnostic_lock:
                        validation_rows = bootstrap_observation.setdefault("candidate_validation_timings", [])
                        if len(validation_rows) < 16:
                            validation_rows.append(validation_summary)
                    print("FWROUTER_ACCEPTANCE_CANDIDATE_VALIDATION " + json.dumps(validation_summary), flush=True)
                print(prefix + json.dumps({
                    "phase": name, "event": "exit", "call": count,
                    "seconds": elapsed, "cpu_seconds": cpu_elapsed,
                }), flush=True)
        return observed

    for phase in (
        "_prospective_profile_bindings",
        "_stage_profile_native_candidates",
        "_apply_staged_mihomo_candidate",
        "_write_xray_generation_checkpoint",
        "_restore_xray_generation_checkpoint",
        "_finalize_xray_profile_publication",
        "_publish_mihomo_udp_receipt_with_owner",
    ):
        setattr(
            xray_subscription_service,
            phase,
            observe_xray_generation_phase(phase, getattr(xray_subscription_service, phase)),
        )
    from fwrouter_api.services import subject_policy
    for module, attribute, phase in (
        (subject_policy, "_default_subject_runtime_enforcement", "subject_policy.default_enforcement"),
        (mihomo_config, "write_mihomo_candidate_config", "mihomo_config.write_candidate"),
        (mihomo_config, "validate_mihomo_candidate_config", "mihomo_config.validate_candidate"),
        (subscription_pipeline, "validate_mihomo_candidate_config", "subscription_pipeline.validate_candidate"),
    ):
        setattr(module, attribute, observe_xray_generation_phase(phase, getattr(module, attribute)))

    def observe_phase(name, original):
        def observed(*args, **kwargs):
            if not bootstrap_diagnostic_active:
                return original(*args, **kwargs)
            started = time.monotonic()
            cpu_started = time.process_time()
            print("FWROUTER_ACCEPTANCE_BOOTSTRAP_PHASE " + json.dumps({
                "phase": name, "event": "enter",
            }), flush=True)
            try:
                return original(*args, **kwargs)
            finally:
                elapsed = round(time.monotonic() - started, 4)
                cpu_elapsed = round(time.process_time() - cpu_started, 4)
                with bootstrap_diagnostic_lock:
                    bootstrap_observation.setdefault("phase_timings", {}).setdefault(name, []).append(elapsed)
                    bootstrap_observation.setdefault("phase_cpu_timings", {}).setdefault(name, []).append(cpu_elapsed)
                print("FWROUTER_ACCEPTANCE_BOOTSTRAP_PHASE " + json.dumps({
                    "phase": name, "event": "exit", "seconds": elapsed,
                    "cpu_seconds": cpu_elapsed,
                }), flush=True)
        return observed

    for phase in ("recover_startup_live_routing_from_persisted_mode", "recover_startup_mihomo_selector",
                  "recover_startup_intended_routing", "recover_startup_scoped_subject_routing",
                  "recover_startup_xray_subscription_profiles", "_run_startup_dnsmasq_reconcile_step"):
        setattr(bootstrap_service, phase, observe_phase(phase, getattr(bootstrap_service, phase)))
    for module, attribute, phase in (
        (bootstrap_service, "_read_startup_dataplane_payload", "startup.read_dataplane_payload"),
        (bootstrap_service, "_wait_for_startup_vpn_dataplane_ready", "startup.wait_vpn_dataplane_ready"),
        (apply_orchestrator, "apply_global_mode_immediately", "startup.apply_global_mode_immediately"),
        (mihomo_reconcile, "promote_mihomo_candidate_config", "startup.promote_mihomo_candidate_config"),
    ):
        setattr(module, attribute, observe_phase(phase, getattr(module, attribute)))

    def phase_summary(value: Any) -> dict[str, Any]:
        value = value if isinstance(value, dict) else {}
        summary = {key: value[key] for key in (
            "ok", "restored", "changed", "skipped", "recovered", "recovery_required", "recovery_skipped",
            "mode_matches", "reapply_required", "reapplied", "runtime_state_repaired", "classify_probe_ok",
        ) if type(value.get(key)) is bool}
        for key in ("persisted_scoped_subjects_count", "missing_scoped_subjects_count"):
            if type(value.get(key)) is int and value[key] >= 0:
                summary[key] = value[key]
        if value.get("inventory_authority") == "persisted_success":
            summary["inventory_authority"] = "persisted_success"
        status = value.get("status")
        if status in {"success", "failed", "skipped", "noop", "unchanged", "disabled", "not_applicable", "blocked"}:
            summary["status"] = status
        error_code = value.get("error_code")
        if isinstance(error_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code):
            summary["error_code"] = error_code
        for key in ("recovery", "apply_result"):
            nested = value.get(key)
            if isinstance(nested, dict):
                summary[key] = {field: nested[field] for field in ("ok", "error_code")
                                if type(nested.get(field)) is bool or (
                                    field == "error_code" and isinstance(nested.get(field), str)
                                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", nested[field]))}
        receipt = value.get("staged_publication_receipt")
        if (isinstance(receipt, dict) and type(receipt.get("schema")) is int
                and receipt.get("schema") == 1
                and receipt.get("staged_native_publication_complete") is True):
            summary["staged_publication_receipt"] = {
                "schema": 1, "staged_native_publication_complete": True,
            }
        generation_apply = value.get("generation_apply")
        if isinstance(generation_apply, dict):
            generation_summary: dict[str, Any] = {}
            final_mihomo = generation_apply.get("final_mihomo")
            if isinstance(final_mihomo, dict) and type(final_mihomo.get("ok")) is bool:
                generation_summary["final_mihomo"] = {"ok": final_mihomo["ok"]}
            if type(generation_apply.get("public_snapshots_changed")) is bool:
                generation_summary["public_snapshots_changed"] = generation_apply["public_snapshots_changed"]
            if generation_summary:
                summary["generation_apply"] = generation_summary
        promotion = value.get("public_profile_promote")
        if isinstance(promotion, dict):
            promotion_summary = {
                key: promotion[key] for key in ("profiles_count", "nodes_count")
                if type(promotion.get(key)) is int and promotion[key] >= 0
            }
            if promotion_summary:
                summary["public_profile_promote"] = promotion_summary
        final_reconcile = value.get("final_mihomo_reconcile")
        if isinstance(final_reconcile, dict):
            reconcile_summary = {
                key: final_reconcile[key] for key in ("ok", "skipped")
                if type(final_reconcile.get(key)) is bool
            }
            if final_reconcile.get("reason") == "staged_publication_native_proof_complete":
                reconcile_summary["reason"] = "staged_publication_native_proof_complete"
            if reconcile_summary:
                summary["final_mihomo_reconcile"] = reconcile_summary
        return summary

    def observed_bootstrap() -> dict[str, Any]:
        nonlocal bootstrap_diagnostic_active
        bootstrap_diagnostic_active = True
        try:
            result = original_bootstrap()
        finally:
            bootstrap_diagnostic_active = False
        bootstrap_observation.update({
            "completed": True,
            "recovery_enabled": result.get("startup_recovery_enabled") is True,
            "foundation_ok": (result.get("database_schema") or {}).get("ok") is True,
            "live_recovery": {key: phase_summary(value) for key, value in
                              (result.get("startup_live_recovery") or {}).items() if key in {"live_routing", "selector"}},
            "apply_reconcile": {key: phase_summary(value) for key, value in
                                (result.get("startup_apply_reconcile") or {}).items() if key in {
                                    "intended_routing", "scoped_subject_routing", "xray_subscription_profiles", "dnsmasq_reconcile"}},
        })
        if acceptance_profile.get("profile") == "hosted-kernel-packet" and result.get("startup_recovery_enabled") is True:
            dns_fixture = _packet_dnsmasq_fixture(state, acceptance_profile)
            bootstrap_observation["dns_fixture"] = {key: dns_fixture.get(key) is True for key in (
                "configured", "daemon_running", "daemon_uid_matches",
            )}
        return result

    application_main.bootstrap_backend = observed_bootstrap
    app = create_app(enable_startup_tasks=os.environ.get("FWROUTER_STARTUP_TASKS_ENABLED") == "1")
    app.add_api_route("/api/v2/__acceptance/bootstrap/report", lambda: dict(bootstrap_observation), methods=["GET"])
    _bind_xray_checkpoint_barrier(socket_path, state)
    mihomo_adapter = _bind_acceptance_mihomo(socket_path, state)
    _install_acceptance_mihomo_fence_observer()
    _install_acceptance_provider_verification_observer()
    _install_acceptance_generation_callback_observer()
    mihomo_delay_observer = _install_acceptance_mihomo_delay_error_observer()
    mihomo_adapter._acceptance_group_probe_observer = mihomo_delay_observer
    _install_acceptance_selection_fence_observer()
    _install_acceptance_mihomo_reconcile_failure_observer()
    _install_acceptance_recovery_apply_observer()
    if acceptance_profile.get("profile") == "hosted-kernel-packet":
        _install_acceptance_xray_generation_rollback_observer()
        _install_acceptance_apply_exception_observer()
        _install_acceptance_live_mode_probe_observer()
        _install_acceptance_preflight_observer()
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
        if payload.get("direct_checkpoint") is True:
            from fwrouter_api.services.xray_subscription_service import (
                _recover_checkpoint_from_current_projection,
                _xray_generation_checkpoint_path,
                _xray_adapter,
            )
            adapter = _xray_adapter()
            return _recover_checkpoint_from_current_projection(
                adapter, _xray_generation_checkpoint_path(adapter),
            )
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
            include_vpn_auto=payload.get("include_vpn_auto") is True,
            **options,
        )

    app.add_api_route("/api/v2/__acceptance/xray/reconcile", recover_xray_generation, methods=["POST"])

    def arm_prepared_checkpoint_failure() -> dict[str, Any]:
        """Fail once after the real prepared checkpoint write has returned."""
        failure_flag = state / "run" / "fail-after-prepared-checkpoint-once"
        descriptor = os.open(
            failure_flag,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        os.close(descriptor)
        return {"ok": True, "armed": True}

    app.add_api_route(
        "/api/v2/__acceptance/xray/checkpoint-failure/arm",
        arm_prepared_checkpoint_failure,
        methods=["POST"],
    )

    def refresh_identity_fixture_profile(payload: dict[str, Any]) -> dict[str, Any]:
        """Run the real subscription inventory upsert for a synthetic JSON profile."""
        if set(payload) != {"profile"} or not isinstance(payload.get("profile"), dict):
            return {"ok": False, "error_code": "IDENTITY_FIXTURE_PROFILE_INVALID"}
        import json as _json
        from fwrouter_api.adapters.subscription import parse_subscription_payload
        from fwrouter_api.services.subscription import _upsert_subscription_servers
        parsed = parse_subscription_payload(_json.dumps(payload["profile"], sort_keys=True))
        if not parsed.servers:
            return {"ok": False, "error_code": "IDENTITY_FIXTURE_PROFILE_UNSUPPORTED"}
        source_url = "https://identity.acceptance.invalid/subscription"
        servers = parsed.servers
        result = _upsert_subscription_servers(
            servers, servers_by_url={source_url: servers},
        )
        return {
            "ok": bool(result.get("ok", True)),
            "server_ids": [str(server.server_id) for server in servers],
            "refreshed_count": len(servers),
        }

    app.add_api_route(
        "/api/v2/__acceptance/xray/identity-refresh",
        refresh_identity_fixture_profile,
        methods=["POST"],
    )

    def identity_manifest_context() -> dict[str, Any]:
        """Return only revision and digest needed to author a private test manifest."""
        from fwrouter_api.db.connection import db_session
        from fwrouter_api.services.subscription_node_identity import canonical_digest
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision
        with db_session() as connection:
            pairs = []
            for row in connection.execute(
                "SELECT json_extract(metadata_json, '$.detail.email') AS email, "
                "COALESCE(json_extract(metadata_json, '$.detail.client_uuid'), "
                "json_extract(metadata_json, '$.detail.client_id')) AS client_uuid "
                "FROM subjects WHERE implementation_kind='xray' AND is_deleted=0 AND is_active=1"
            ):
                if row["email"] and row["client_uuid"]:
                    pairs.append((str(row["client_uuid"]), str(row["email"])))
            return {
                "ok": True,
                "selection_revision": read_selection_revision(connection),
                "identity_digest": canonical_digest(sorted(pairs)),
            }

    app.add_api_route(
        "/api/v2/__acceptance/xray/identity-context",
        identity_manifest_context,
        methods=["GET"],
    )

    def revoke_identity_fixture_credential(payload: dict[str, Any]) -> dict[str, Any]:
        """Tombstone one fixed synthetic public credential through the real service helper."""
        expected_scope = "subscription:identity-public-0@acceptance.invalid"
        expected_server = str(payload.get("server_id") or "")
        if set(payload) != {"server_id"} or not expected_server.startswith("sub:"):
            return {"ok": False, "error_code": "IDENTITY_FIXTURE_REVOCATION_INVALID"}
        from fwrouter_api.db.connection import db_session
        from fwrouter_api.adapters.xray_common import xray_writer_guard
        from fwrouter_api.services.subscription_node_identity import revoke_credentials
        with xray_writer_guard(timeout_seconds=30.0):
            with db_session() as connection:
                row = connection.execute(
                    "SELECT identity_id FROM subscription_node_aliases WHERE server_id=?",
                    (expected_server,),
                ).fetchone()
                if row is None:
                    return {"ok": False, "error_code": "IDENTITY_FIXTURE_CREDENTIAL_UNAVAILABLE"}
                credential = connection.execute(
                    "SELECT epoch FROM subscription_node_credentials WHERE scope=? AND identity_id=?",
                    (expected_scope, row["identity_id"]),
                ).fetchone()
                if credential is None:
                    return {"ok": False, "error_code": "IDENTITY_FIXTURE_CREDENTIAL_UNAVAILABLE"}
                result = revoke_credentials(
                    connection, scope=expected_scope, identity_id=str(row["identity_id"]),
                    expected_epoch=int(credential["epoch"]),
                )
        return {"ok": True, "revoked": result.get("revoked") == 1,
                "epoch": result.get("epoch")}

    app.add_api_route(
        "/api/v2/__acceptance/xray/identity-revoke",
        revoke_identity_fixture_credential,
        methods=["POST"],
    )

    def run_identity_cli(payload: dict[str, Any]) -> dict[str, Any]:
        """Exercise the protected CLI in this fixture's patched, owned runtime."""
        import contextlib
        import io
        import json as _json
        import os as _os
        from fwrouter_api.cli import main as cli_main
        action = payload.get("action")
        if action in {"dry-run", "apply"}:
            manifest = payload.get("manifest")
            if not isinstance(manifest, dict):
                return {"ok": False, "error_code": "IDENTITY_MANIFEST_INVALID"}
            path = state / "run" / "identity-migration-manifest.json"
            raw = _json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            descriptor = _os.open(path, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | _os.O_CLOEXEC, 0o600)
            try:
                with _os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(raw)
                    stream.flush()
                    _os.fsync(stream.fileno())
            finally:
                _os.close(descriptor)
            try:
                argv = ["xray", "identity-migration", "--manifest", str(path)]
                if action == "apply":
                    plan = payload.get("expected_plan_sha256")
                    if not isinstance(plan, str):
                        return {"ok": False, "error_code": "IDENTITY_MIGRATION_PLAN_DIGEST_REQUIRED"}
                    argv += ["--apply", "--expected-plan-sha256", plan]
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    exit_code = cli_main(argv)
                try:
                    receipt = _json.loads(output.getvalue())
                except ValueError:
                    return {"ok": False, "error_code": "IDENTITY_CLI_RECEIPT_INVALID", "exit_code": exit_code}
                return {"ok": exit_code == 0 and receipt.get("ok") is True,
                        "exit_code": exit_code, "stderr_empty": not errors.getvalue(),
                        "receipt": receipt}
            finally:
                path.unlink(missing_ok=True)
        if action == "supersede":
            checkpoint_sha = payload.get("checkpoint_sha256")
            plan_sha = payload.get("migration_plan_sha256")
            if not isinstance(checkpoint_sha, str) or not isinstance(plan_sha, str):
                return {"ok": False, "error_code": "XRAY_SUPERSESSION_PROOF_REQUIRED"}
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                exit_code = cli_main([
                    "xray", "generation", "supersede",
                    "--checkpoint-sha256", checkpoint_sha,
                    "--migration-plan-sha256", plan_sha,
                    "--apply",
                ])
            try:
                receipt = _json.loads(output.getvalue())
            except ValueError:
                return {"ok": False, "error_code": "XRAY_SUPERSESSION_RECEIPT_INVALID", "exit_code": exit_code}
            return {"ok": exit_code == 0 and receipt.get("ok") is True,
                    "exit_code": exit_code, "stderr_empty": not errors.getvalue(),
                    "receipt": receipt}
        return {"ok": False, "error_code": "IDENTITY_CLI_ACTION_INVALID"}

    app.add_api_route(
        "/api/v2/__acceptance/xray/identity-cli",
        run_identity_cli,
        methods=["POST"],
    )

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
            import subprocess
            from fwrouter_api.services import dnsmasq
            current = _PACKET_DNSMASQ_STATE
            runtime = current.get("runtime") or {}
            process = runtime.get("process")
            try:
                # This acceptance read must observe the current daemon/capture/nftset
                # state, not a short-lived status-cache entry from before reconciliation.
                status = dnsmasq._inspect_dnsmasq_selective_status_uncached()
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
            nft_probe_count = min(len(nft_probe), 3) if isinstance(nft_probe, list) else 0
            nft_probe_status = (status.get("nftset_probe_status") or {}).get("status")
            if nft_probe_status not in {"ok", "unhealthy", "not_applicable"}:
                nft_probe_status = "unknown"
            capture_status = status.get("dns_capture_status")
            capture_status = capture_status if isinstance(capture_status, dict) else {}
            capture_missing_codes = []
            raw_capture_missing = capture_status.get("missing")
            capture_missing_unknown_count = 0
            capture_missing_truncated = False
            if isinstance(raw_capture_missing, list):
                capture_missing_truncated = len(raw_capture_missing) > 16
                for missing in raw_capture_missing[:16]:
                    code = None
                    if missing == "router_dns_interfaces_missing":
                        code = missing
                    elif isinstance(missing, str) and missing.startswith("dns_capture_udp_missing:"):
                        code = "dns_capture_udp_missing"
                    elif isinstance(missing, str) and missing.startswith("dns_capture_tcp_missing:"):
                        code = "dns_capture_tcp_missing"
                    if code is None:
                        capture_missing_unknown_count = min(2, capture_missing_unknown_count + 1)
                    else:
                        capture_missing_codes.append(code)
            capture_rules = capture_status.get("rules_present")
            capture_rule_count = min(len(capture_rules), 2) if isinstance(capture_rules, list) else 0
            capture_rule_status = []
            if isinstance(capture_rules, list):
                for item in capture_rules[:1]:
                    if isinstance(item, dict):
                        capture_rule_status.append({"udp": item.get("udp") is True,
                                                    "tcp": item.get("tcp") is True})
            rescue_receipt = current.get("rescue_discovery_receipt")
            rescue_receipt = rescue_receipt if isinstance(rescue_receipt, dict) else {}

            def bounded_config_text(path: Path) -> str:
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    return ""
                return text if len(text.encode("utf-8")) <= 8192 else ""

            dhcp_text = bounded_config_text(Path(dnsmasq.DNSMASQ_DHCP_DNS_CONF_PATH))
            hosts_text = bounded_config_text(Path(dnsmasq.DNSMASQ_LOCAL_HOSTS_CONF_PATH))
            discovered_interfaces = status.get("router_dns_interfaces")
            discovered_addresses = status.get("router_dns_ipv4")
            capture_interfaces = capture_status.get("interfaces")

            # Query the existing native listener through its LAN address. The
            # separate packet client remains the proof for external LAN DNS.
            try:
                hostname_query = subprocess.run(
                    ["/usr/bin/dig", "-b", "10.240.0.1", "@10.240.0.1", "fwrouter.lan", "A",
                     "+short", "+time=1", "+tries=1"],
                    check=False, capture_output=True, timeout=2, stdin=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "TZ": "UTC"},
                )
            except subprocess.TimeoutExpired:
                hostname_query = None
            hostname_output = hostname_query.stdout if hostname_query is not None else b""
            hostname_error = hostname_query.stderr if hostname_query is not None else b""
            hostname_answer = (hostname_output.decode("ascii", "ignore").strip()
                               if len(hostname_output) <= 1024 else "")
            return {
                "configured": bool(current.get("configured")),
                "daemon_running": process is not None and process.poll() is None,
                "daemon_uid_matches": process is not None and _dnsmasq_native_uid_matches(process),
                "native_process_arguments_match": argv_matches,
                "selective_status_ok": status.get("ok") is True,
                "dns_capture_ok": (status.get("dns_capture_status") or {}).get("ok") is True,
                "nftset_probe_ok": (status.get("nftset_probe_status") or {}).get("ok") is True,
                "dns_capture_missing_codes": sorted(set(capture_missing_codes)),
                "dns_capture_missing_unknown_count_capped": capture_missing_unknown_count,
                "dns_capture_missing_truncated": capture_missing_truncated,
                "dns_capture_rule_count_capped": capture_rule_count,
                "dns_capture_rules_present": capture_rule_status,
                "rescue0_allowlisted": rescue_receipt.get("rescue0_allowlisted") is True,
                "rescue0_role_excluded_from_discovery": rescue_receipt.get("rescue0_role_excluded") is True,
                "router_dns_interfaces": discovered_interfaces[:8]
                if isinstance(discovered_interfaces, list) and all(isinstance(item, str) for item in discovered_interfaces[:8]) else [],
                "router_dns_ipv4": discovered_addresses[:8]
                if isinstance(discovered_addresses, list) and all(isinstance(item, str) for item in discovered_addresses[:8]) else [],
                "dhcp_excludes_rescue_vip": "10.254.254.1" not in dhcp_text,
                "dhcp_forces_router_dns": "dhcp-option-force=option:dns-server,10.240.0.1" in dhcp_text,
                "local_hosts_exclude_rescue_vip": "10.254.254.1" not in hosts_text,
                "dns_capture_interfaces": capture_interfaces[:8]
                if isinstance(capture_interfaces, list) and all(isinstance(item, str) for item in capture_interfaces[:8]) else [],
                "dns_capture_excludes_rescue0": isinstance(capture_interfaces, list)
                and "rescue0" not in capture_interfaces,
                "router_lan_hostname_query_ok": hostname_query is not None
                and hostname_query.returncode == 0 and len(hostname_output) <= 1024 and len(hostname_error) <= 1024
                and hostname_answer == "10.240.0.1",
                "router_lan_hostname_query_answer_matches": hostname_answer == "10.240.0.1",
                "nftset_probe_status": nft_probe_status,
                "nftset_probe_target_count_capped": nft_probe_count,
                "nftset_probe_targets_truncated": isinstance(nft_probe, list) and len(nft_probe) > 4,
                "vpn_rule_generated": "server=/vpn.test/1.1.1.1" in lines,
                "upstream_domains": sorted(set(upstream_domains)),
                "unexpected_upstream_rule_count": unexpected_upstream_rules,
                "vpn_nftset_generated": any(line.startswith("nftset=/vpn.test/4#") and line.endswith("#dns_vpn_ipv4") for line in lines),
                "direct_rule_generated": not any(line.startswith("server=/direct.test/") for line in lines),
                "direct_nftset_generated": any(line.startswith("nftset=/direct.test/4#") and line.endswith("#dns_direct_ipv4") for line in lines),
                "nft_probe_targets": nft_proof,
                "process_exit_code": process.returncode if process is not None and process.poll() is not None else None,
                "startup_error_code": runtime.get("startup_error_code") if runtime.get("startup_error_code") in {
                    "native_uid_mismatch", "native_process_exited", "readiness_probe_timeout",
                    "readiness_deadline"} else None,
                "config_test_status": runtime.get("config_test_status") if runtime.get("config_test_status") in {
                    "passed", "failed", "timeout"} else None,
                "config_test_returncode": runtime.get("config_test_returncode")
                if type(runtime.get("config_test_returncode")) is int
                and -255 <= runtime["config_test_returncode"] <= 255 else None,
                "config_test_stdout_bytes": runtime.get("config_test_stdout_bytes")
                if type(runtime.get("config_test_stdout_bytes")) is int else None,
                "config_test_stderr_bytes": runtime.get("config_test_stderr_bytes")
                if type(runtime.get("config_test_stderr_bytes")) is int else None,
                "config_test_error_code": runtime.get("config_test_error_code") if runtime.get("config_test_error_code") in {
                    "config_test_timeout", "config_test_output_bound_exceeded", "config_test_rejected"} else None,
                "reconcile_result": current.get("reconcile_result")
                if isinstance(current.get("reconcile_result"), dict) else None,
                "diagnostic_tail": _dnsmasq_diagnostic_tail(runtime),
                "lan_interface_count": 1 if current else 0,
            }

        def stop_packet_dns53() -> dict[str, Any]:
            return _packet_dnsmasq_fixture(state, acceptance_profile, stop=True)

        app.add_api_route("/api/v2/__acceptance/dns53/configure", configure_packet_dns53, methods=["POST"])
        app.add_api_route("/api/v2/__acceptance/dns53/status", packet_dns53_status, methods=["GET"])
        app.add_api_route("/api/v2/__acceptance/dns53/stop", stop_packet_dns53, methods=["POST"])
        if os.environ.get("FWROUTER_STARTUP_RECOVERY_ENABLED") == "1":
            _packet_dnsmasq_fixture(state, acceptance_profile)
    return app


def _bind_xray_checkpoint_barrier(socket_path: Path, state: Path) -> None:
    """Expose post-write barriers and one-shot durable-checkpoint fault injection."""
    if (os.environ.get("FWROUTER_ENVIRONMENT") != "test"
            or os.environ.get("FWROUTER_APPLICATION_ACCEPTANCE_ROOT") != "/tmp/fwrouter-application-acceptance"):
        raise RuntimeError("Xray checkpoint barriers require the qualified test worker")
    from fwrouter_api.services import xray_subscription_service

    real_fsync_directory = xray_subscription_service._fsync_directory
    original_write_checkpoint = xray_subscription_service._write_xray_generation_checkpoint
    checkpoint_path = state / "xray" / ".generation" / "generation-checkpoint.json"
    fail_after_prepared_write = state / "run" / "fail-after-prepared-checkpoint-once"

    def write_checkpoint_then_inject(*args: Any, **kwargs: Any) -> Any:
        result = original_write_checkpoint(*args, **kwargs)
        try:
            value = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            should_fail = value.get("phase") == "prepared" and fail_after_prepared_write.is_file()
        except (OSError, ValueError, TypeError):
            should_fail = False
        if should_fail:
            fail_after_prepared_write.unlink(missing_ok=True)
            raise RuntimeError("ACCEPTANCE_PREPARED_CHECKPOINT_POSTWRITE_FAILURE")
        return result

    xray_subscription_service._write_xray_generation_checkpoint = write_checkpoint_then_inject

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

    original_generated_logical_profile_group = mihomo_config_proxies._generated_logical_profile_group
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

    def generated_logical_profile_group_on_owned_bridge(
        runtime_name: str, members: list[dict[str, Any]], *args: Any, **kwargs: Any,
    ) -> dict[str, Any]:
        generated = original_generated_logical_profile_group(runtime_name, members, *args, **kwargs)
        return _owned_logical_health_check_groups(
            [generated], owned_probe_url, profile=acceptance_profile,
            expected_probe_port=owned_probe_port)[0]

    mihomo_config_proxies._generated_logical_profile_group = generated_logical_profile_group_on_owned_bridge

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

        def _probe_result_barrier(self, target: str, result: Any,
                                  delay_request_evidence: dict[str, Any] | None = None) -> None:
            payload = {
                "logical_runtime_target": str(target),
                "probe_ok": bool(result.get("ok")) if isinstance(result, dict) else bool(getattr(result, "ok", False)),
            }
            if isinstance(delay_request_evidence, dict):
                payload["delay_request_evidence"] = delay_request_evidence
            reply = _rpc_call(socket_path, "mihomo_probe_result", payload)
            if not reply.get("ok"):
                raise RuntimeError("acceptance native probe-result barrier failed")

        def probe_logical_group(self, logical_runtime_target: str, *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> dict[str, Any]:
            owned_url = self._acceptance_probe_url(test_url)
            observer = getattr(self, "_acceptance_group_probe_observer", None)
            token = (observer["begin_probe"]("group", logical_runtime_target, owned_url)
                     if isinstance(observer, dict) else None)
            result = None
            try:
                result = super().probe_logical_group(
                    logical_runtime_target, test_url=owned_url, timeout_ms=timeout_ms,
                )
            finally:
                evidence = (observer["finish_group_probe"](token, result)
                            if token is not None and isinstance(observer, dict) else None)
            self._probe_result_barrier(logical_runtime_target, result, evidence)
            return result

        def probe_logical_member(self, logical_runtime_target: str, member_runtime_identity: str, *, test_url: str = "https://www.gstatic.com/generate_204", timeout_ms: int = 5000) -> dict[str, Any]:
            owned_url = self._acceptance_probe_url(test_url)
            observer = getattr(self, "_acceptance_group_probe_observer", None)
            token = (observer["begin_probe"]("member", member_runtime_identity, owned_url)
                     if isinstance(observer, dict) else None)
            result = None
            try:
                result = super().probe_logical_member(
                    logical_runtime_target, member_runtime_identity, test_url=owned_url, timeout_ms=timeout_ms,
                )
            finally:
                evidence = (observer["finish_group_probe"](token, result)
                            if token is not None and isinstance(observer, dict) else None)
            self._probe_result_barrier(logical_runtime_target, result, evidence)
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

    graph_diagnostic_state = {"records": 0}
    graph_diagnostic_lock = threading.Lock()

    def record_candidate_graph_failure(candidate: Path, *, stderr: str = "") -> None:
        """Emit bounded name-hash-only context before a rejected candidate is discarded."""
        with graph_diagnostic_lock:
            if graph_diagnostic_state["records"] >= 1:
                return
            graph_diagnostic_state["records"] += 1

        def digest(value: Any) -> str:
            return hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()[:16]

        def summarize_config(path: Path) -> dict[str, Any]:
            import yaml
            root = Path(os.environ["FWROUTER_APPLICATION_ACCEPTANCE_ROOT"]).resolve()
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root) or resolved.stat().st_size > 8 * 1024 * 1024:
                    return {"available": False, "reason": "outside_or_oversized"}
                value = yaml.load(resolved.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
            except Exception:
                return {"available": False, "reason": "unreadable_or_invalid"}
            if not isinstance(value, dict):
                return {"available": False, "reason": "not_mapping"}
            proxies = value.get("proxies") if isinstance(value.get("proxies"), list) else []
            groups = value.get("proxy-groups") if isinstance(value.get("proxy-groups"), list) else []
            proxy_names = {str(item.get("name")) for item in proxies if isinstance(item, dict) and item.get("name")}
            group_names = {str(item.get("name")) for item in groups if isinstance(item, dict) and item.get("name")}
            graph = []
            missing = []
            for group in groups[:24]:
                if not isinstance(group, dict) or not isinstance(group.get("name"), str):
                    continue
                members = group.get("proxies") if isinstance(group.get("proxies"), list) else []
                member_names = [str(item) for item in members[:48]]
                unresolved = [name for name in member_names if name not in proxy_names | group_names | {"DIRECT", "REJECT", "PASS", "COMPATIBLE"}]
                missing.extend(unresolved)
                graph.append({
                    "group_name_sha256": digest(group["name"]),
                    "group_type": str(group.get("type") or "")[:32],
                    "shape_sha256": hashlib.sha256(json.dumps(group, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
                    "member_count": len(members),
                    "member_name_sha256": [digest(name) for name in member_names],
                    "missing_member_sha256": [digest(name) for name in unresolved],
                })
            return {
                "available": True, "proxy_count": len(proxies), "group_count": len(groups),
                "proxy_name_sha256": sorted(digest(item.get("name")) for item in proxies[:128] if isinstance(item, dict) and item.get("name")),
                "groups": graph, "missing_member_sha256": sorted({digest(name) for name in missing}),
                "groups_truncated": len(groups) > 24,
                "group_members_truncated": any(len(group.get("member_name_sha256") or []) >= 48 for group in graph),
                "proxies_truncated": len(proxies) > 128,
            }

        record: dict[str, Any] = {
            "schema": "fwrouter-acceptance-mihomo-candidate-graph/v1",
            "event": "native_candidate_validation_failed",
            "native_error_present": bool(stderr),
            "candidate": summarize_config(candidate),
        }
        try:
            from fwrouter_api.services import mihomo_config
            active = Path(mihomo_config._resolved_base_config_path())
            record["active_base"] = summarize_config(active)
        except Exception:
            record["active_base"] = {"available": False, "reason": "source_unavailable"}
        try:
            from fwrouter_api.db.connection import db_session
            from fwrouter_api.services.mihomo_config_proxies import _logical_profile_members, _generated_logical_profile_group
            inactive_rows: list[dict[str, Any]] = []
            inventory_counts = {"active": 0, "missing": 0, "deleted": 0, "other": 0}
            with db_session() as connection:
                rows = connection.execute(
                    "SELECT server_id, server_name, raw_json, inventory_state FROM servers ORDER BY server_id LIMIT 1000"
                ).fetchall()
                for row in rows:
                    state_name = str(row["inventory_state"] or "other")
                    inventory_counts[state_name if state_name in inventory_counts else "other"] += 1
                    if state_name == "active" or len(inactive_rows) >= 20:
                        continue
                    try:
                        raw = json.loads(row["raw_json"] or "{}")
                    except (TypeError, ValueError):
                        continue
                    topology = raw.get("_fwrouter_topology") if isinstance(raw, dict) else None
                    if not isinstance(topology, dict) or topology.get("kind") != "logical_profile":
                        continue
                    runtime_name = str(raw.get("_fwrouter_runtime_name") or raw.get("name") or row["server_name"] or "").strip()
                    members = _logical_profile_members(raw, logical_runtime_name=runtime_name) if runtime_name else []
                    expected_group = _generated_logical_profile_group(runtime_name, members) if members else None
                    member_names = [str(item.get("name") or "") for item in members]
                    inactive_rows.append({
                        "server_id_sha256": digest(row["server_id"]),
                        "runtime_group_name_sha256": digest(runtime_name),
                        "inventory_state": state_name,
                        "runtime_policy": str(topology.get("runtime_policy") or "fallback")[:32],
                        "topology_member_count": len((topology.get("endpoints") or []) if isinstance(topology.get("endpoints"), list) else []),
                        "relational_member_count": len(members),
                        "member_name_sha256": sorted(digest(name) for name in member_names),
                        "generated_shape_sha256": hashlib.sha256(json.dumps(expected_group, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest() if expected_group else None,
                    })
            record["persisted_profile_ownership"] = {
                "inventory_counts": inventory_counts, "inactive_logical_profiles": inactive_rows,
                "rows_truncated": len(rows) >= 1000 or len(inactive_rows) >= 20,
            }
        except Exception as exc:
            record["persisted_profile_ownership"] = {"available": False, "exception_type": type(exc).__name__[:64]}
        try:
            from fwrouter_api.services.mihomo_config_inbounds import _collect_xray_handoff_assignments
            assignments = _collect_xray_handoff_assignments()
            record["required_handoff_targets"] = [{
                "selected_server_id_sha256": digest(item.get("selected_server_id")),
                "proxy_name_sha256": digest(item.get("proxy")),
                "listener_name_sha256": digest(item.get("listener_name")),
                "client_count": len(item.get("client_emails") or []),
            } for item in assignments[:20] if isinstance(item, dict)]
            record["required_handoff_targets_truncated"] = len(assignments) > 20
        except Exception as exc:
            record["required_handoff_targets"] = []
            record["handoff_source_exception_type"] = type(exc).__name__[:64]
        try:
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 12 * 1024:
                compact = {
                    "schema": record["schema"], "event": record["event"],
                    "diagnostic_truncated": True,
                    "candidate_graph_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                    "candidate": {key: record["candidate"].get(key) for key in ("available", "proxy_count", "group_count", "missing_member_sha256")},
                    "active_base": {key: record["active_base"].get(key) for key in ("available", "proxy_count", "group_count", "missing_member_sha256")},
                    "inventory_counts": record.get("persisted_profile_ownership", {}).get("inventory_counts"),
                    "inactive_profile_count": len(record.get("persisted_profile_ownership", {}).get("inactive_logical_profiles", [])),
                    "handoff_target_count": len(record.get("required_handoff_targets", [])),
                }
                record = compact
                encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) <= 12 * 1024:
                root = Path(os.environ["FWROUTER_APPLICATION_ACCEPTANCE_ROOT"]).resolve()
                state_dir = Path(os.environ["FWROUTER_STATE_DIR"]).resolve()
                if not state_dir.is_relative_to(root):
                    raise RuntimeError("candidate graph receipt state path is outside acceptance root")
                output = state_dir / "run" / "mihomo-candidate-graph.json"
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(output, flags, 0o600)
                try:
                    os.fchmod(descriptor, 0o600)
                    payload = (encoded + "\n").encode("utf-8")
                    offset = 0
                    while offset < len(payload):
                        written = os.write(descriptor, payload[offset:])
                        if written <= 0:
                            raise OSError("short write while persisting candidate graph receipt")
                        offset += written
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except Exception as exc:
            try:
                sys.stderr.write("FWROUTER_ACCEPTANCE_MIHOMO_CANDIDATE_GRAPH_WRITE_FAILED "
                                 + type(exc).__name__[:64] + "\n")
                sys.stderr.flush()
            except Exception:
                pass

    def validate_candidate(candidate_path: str | None = None, *, image_reference: str | None = None) -> dict[str, Any]:
        path = str(candidate_path or default_candidate)
        reply = _rpc_call(socket_path, "mihomo_test_config", {"path": path})
        details = reply.get("details") if isinstance(reply.get("details"), dict) else {}
        if not reply.get("ok"):
            record_candidate_graph_failure(Path(path), stderr=str(details.get("stderr") or ""))
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
    def cleanup_native_fixtures() -> None:
        if profile.get("profile") == "hosted-kernel-packet" and _PACKET_DNSMASQ_STATE:
            _packet_dnsmasq_fixture(Path(os.environ["FWROUTER_STATE_DIR"]), profile, stop=True)
        if remove_handoff_alias is not None:
            remove_handoff_alias()

    try:
        app = build_app(socket_path)
        from contextlib import asynccontextmanager
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def observed_lifespan(application):
            try:
                async with original_lifespan(application) as lifespan_state:
                    yield lifespan_state
                if any(thread.is_alive() and thread.name.startswith("fwrouter-")
                       for thread in threading.enumerate()):
                    raise RuntimeError("owned application worker survived lifespan cleanup")
            finally:
                # Uvicorn can terminate itself before the outer Python finally.
                cleanup_native_fixtures()
            # Uvicorn 0.34 re-raises the captured SIGTERM after clean shutdown.
            # This marker is emitted only after actual application cleanup.
            print("FWROUTER_ACCEPTANCE_LIFESPAN_SHUTDOWN_COMPLETE", flush=True)

        app.router.lifespan_context = observed_lifespan
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    finally:
        cleanup_native_fixtures()


if __name__ == "__main__":
    main()
