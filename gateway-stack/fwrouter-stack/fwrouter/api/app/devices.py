from __future__ import annotations

import os
import re
import time
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .routing import read_device_overrides, get_global
from .device_names import (
    get_name as get_device_name,
    get_name_ip as get_device_name_ip,
    touch_seen as touch_device_seen,
    touch_seen_ip as touch_device_seen_ip,
    cleanup_old as cleanup_device_names,
)

MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

DPKG_SUFFIXES = (".dpkg-dist", ".dpkg-old", ".dpkg-new", ".dpkg-bak")


def _read_kv_conf(path: Path) -> Dict[str, str]:
    """
    Very small key=value parser for /etc/fwrouter/fwrouter.conf.
    - ignores blank lines and comments (# or ;)
    - keeps last value if duplicates
    """
    out: Dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip().lower()] = v.strip()
    except FileNotFoundError:
        return out
    except Exception:
        # silent by design
        return out
    return out


def _list_dnsmasq_conf_files(conf_dir: Path) -> List[Path]:
    files: List[Path] = []
    if not conf_dir.exists() or not conf_dir.is_dir():
        return files

    # Ignore known backup dir on minis: /etc/dnsmasq.d/_disabled_baks :contentReference[oaicite:4]{index=4}
    for p in sorted(conf_dir.iterdir(), key=lambda x: x.name):
        try:
            if p.is_dir():
                # skip disabled backup buckets
                if p.name == "_disabled_baks":
                    continue
                continue
            if p.name.startswith("."):
                continue
            if any(p.name.endswith(s) for s in DPKG_SUFFIXES):
                continue
            files.append(p)
        except Exception:
            continue
    return files


def _discover_leasefile(fw_conf: Dict[str, str], dnsmasq_sources: List[Path]) -> Optional[Path]:
    # 1) explicit override
    v = fw_conf.get("dnsmasq_leasefile")
    if v:
        p = Path(v)
        if p.exists() and os.access(p, os.R_OK):
            return p

    # 2) try to find dhcp-leasefile=... in dnsmasq config sources
    for src in dnsmasq_sources:
        try:
            for raw in src.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("dhcp-leasefile="):
                    candidate = Path(line.split("=", 1)[1].strip())
                    if candidate.exists() and os.access(candidate, os.R_OK):
                        return candidate
        except Exception:
            continue

    # 3) standard known locations — BUT only if реально существует и читается
    candidates = [
        Path("/var/lib/misc/dnsmasq.leases"),  # minis expected :contentReference[oaicite:5]{index=5}
        Path("/var/run/dnsmasq.leases"),
        Path("/var/lib/dnsmasq/dnsmasq.leases"),
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.R_OK):
            return p

    return None


def _discover_dnsmasq_sources(fw_conf: Dict[str, str]) -> Tuple[List[Path], List[Path]]:
    """
    Returns (conf_files, reservation_sources)
    - conf_files: files that may contain dhcp-leasefile= or conf-dir
    - reservation_sources: files where we search dhcp-host=
    """
    conf_files: List[Path] = []
    reservation_sources: List[Path] = []

    conf_file_override = fw_conf.get("dnsmasq_conf_file")
    conf_dir_override = fw_conf.get("dnsmasq_conf_dir")

    # main config file
    main_conf = Path(conf_file_override) if conf_file_override else Path("/etc/dnsmasq.conf")
    if main_conf.exists() and os.access(main_conf, os.R_OK):
        conf_files.append(main_conf)

    # config dir
    conf_dir = Path(conf_dir_override) if conf_dir_override else Path("/etc/dnsmasq.d")
    dir_files = _list_dnsmasq_conf_files(conf_dir)
    conf_files.extend(dir_files)
    reservation_sources.extend(dir_files)

    # Fallback: if /etc/dnsmasq.d missing, still try /etc/dnsmasq.conf for dhcp-host lines
    if not reservation_sources and main_conf.exists() and os.access(main_conf, os.R_OK):
        reservation_sources.append(main_conf)

    return conf_files, reservation_sources


def _parse_dnsmasq_leases(path: Path) -> List[Dict[str, Any]]:
    """
    dnsmasq leases line format:
      <expiry_epoch> <mac> <ip> <hostname> <clientid>
    Active if expiry_epoch == 0 or expiry_epoch > now.
    """
    out: List[Dict[str, Any]] = []
    now = int(time.time())

    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                expiry = int(parts[0])
            except Exception:
                continue
            mac = parts[1].lower()
            ip = parts[2]
            hostname = parts[3]
            if hostname == "*":
                hostname = ""

            is_active = (expiry == 0) or (expiry > now)
            if not is_active:
                continue

            out.append(
                {
                    "mac": mac,
                    "ip": ip,
                    "hostname": hostname,
                    "expiry_epoch": expiry,
                }
            )
    except FileNotFoundError:
        return []
    except Exception:
        # silent by design
        return []

    # De-dup by MAC: keep the freshest lease for a device to avoid duplicates
    by_mac: Dict[str, Dict[str, Any]] = {}
    for d in out:
        mac = d.get("mac") or ""
        if not mac:
            continue
        if mac not in by_mac:
            by_mac[mac] = d
            continue
        cur = by_mac[mac]
        # expiry==0 means "infinite", treat as max
        def exp_val(x: Dict[str, Any]) -> int:
            v = int(x.get("expiry_epoch") or 0)
            return (1 << 62) if v == 0 else v
        if exp_val(d) > exp_val(cur):
            by_mac[mac] = d
        elif exp_val(d) == exp_val(cur) and (not cur.get("hostname")) and d.get("hostname"):
            by_mac[mac] = d

    # include entries with no mac as-is (rare)
    no_mac = [d for d in out if not d.get("mac")]
    return list(by_mac.values()) + no_mac


def _extract_mac(parts: List[str]) -> Optional[str]:
    for p in parts:
        p = p.strip()
        if MAC_RE.match(p):
            return p.lower()
    return None


def _extract_ip(parts: List[str]) -> Optional[str]:
    for p in parts:
        p = p.strip()
        if IPV4_RE.match(p):
            return p
    return None


def _parse_dhcp_host_line(value: str) -> Optional[Dict[str, Any]]:
    """
    dhcp-host can have multiple forms.
    We do a best-effort extraction:
      - MAC: first token matching xx:xx:xx:xx:xx:xx
      - IPv4: first token matching a.b.c.d
      - name: first remaining token that's not a tag/set/clientid-ish prefix
    """
    parts = [p.strip() for p in value.split(",") if p.strip()]
    mac = _extract_mac(parts)
    ip = _extract_ip(parts)

    if not mac:
        return None


def _tailscale_active_peers() -> List[Dict[str, Any]]:
    """
    Best-effort list of active tailscale peers for UI (IPs only).
    Silent on any errors.
    """
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return []

    if proc.returncode != 0 or not proc.stdout:
        return []

    try:
        data = json.loads(proc.stdout)
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    peers = data.get("Peer", {}) or {}
    for _, p in peers.items():
        if p.get("Online") is not True:
            continue
        ips = p.get("TailscaleIPs") or []
        name = p.get("DNSName") or p.get("HostName") or ""
        for ip in ips:
            if not IPV4_RE.match(ip):
                continue
            out.append(
                {
                    "mac": "",
                    "ip": ip,
                    "hostname": name,
                    "expiry_epoch": 0,
                    "source": "tailscale",
                }
            )
    return out


def _tailscale_names() -> Dict[str, str]:
    """
    Read cached tailscale peer names (by IP).
    """
    path = Path("/var/lib/fwrouter/tailscale_peers.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    out: Dict[str, str] = {}
    if isinstance(data, dict):
        for ip, meta in data.items():
            if not isinstance(meta, dict):
                continue
            name = str(meta.get("name") or "").strip()
            if name:
                out[ip] = name
    return out


def _tailscale_cached_peers() -> List[Dict[str, Any]]:
    """
    Use cached tailscale peer list (no tailscale binary needed).
    """
    path = Path("/var/lib/fwrouter/tailscale_peers.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        for ip, meta in data.items():
            if not IPV4_RE.match(str(ip)):
                continue
            name = ""
            if isinstance(meta, dict):
                name = str(meta.get("name") or "").strip()
            out.append(
                {
                    "mac": "",
                    "ip": str(ip),
                    "hostname": name,
                    "expiry_epoch": 0,
                    "source": "tailscale-cache",
                }
            )
    return out

    name = ""
    for p in parts:
        if p.lower() == mac:
            continue
        if ip and p == ip:
            continue
        # skip typical prefixes
        if p.startswith("set:") or p.startswith("tag:") or p.startswith("id:"):
            continue
        if p == "ignore":
            continue
        # if looks like a netmask or lease time, skip
        if "/" in p:
            continue
        if p.isdigit():
            continue
        # take first reasonable string token
        name = p
        break

    return {"mac": mac, "ip": ip or "", "name": name}


def _parse_dnsmasq_reservations(sources: List[Path]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    for src in sources:
        try:
            for raw in src.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # drop inline comments
                if "#" in line:
                    line = line.split("#", 1)[0].strip()
                if "dhcp-host=" not in line:
                    continue
                _, rhs = line.split("dhcp-host=", 1)
                parsed = _parse_dhcp_host_line(rhs.strip())
                if parsed:
                    parsed["source"] = str(src)
                    out.append(parsed)
        except Exception:
            continue

    # de-dup by MAC (keep last occurrence)
    by_mac: Dict[str, Dict[str, Any]] = {}
    for r in out:
        by_mac[r["mac"]] = r
    return list(by_mac.values())


def get_devices_snapshot() -> Dict[str, Any]:
    """
    Main public function for API:
    - "active": from leases (read-only)
    Silent behaviour: if files missing/unreadable → empty lists, no errors.
    """
    fw_conf = _read_kv_conf(Path("/etc/fwrouter/fwrouter.conf"))
    conf_files, _reservation_sources = _discover_dnsmasq_sources(fw_conf)
    leasefile = _discover_leasefile(fw_conf, conf_files)

    active: List[Dict[str, Any]] = []
    if leasefile:
        active = _parse_dnsmasq_leases(leasefile)

    # Merge in tailscale active peers (if any)
    tailscale_active = _tailscale_active_peers()
    if not tailscale_active:
        tailscale_active = _tailscale_cached_peers()
    if tailscale_active:
        existing_ips = {d.get("ip") for d in active if d.get("ip")}
        for d in tailscale_active:
            if d.get("ip") in existing_ips:
                continue
            active.append(d)

    overrides = read_device_overrides()
    global_mode = get_global().get("mode", "DIRECT")

    # Keep custom device names for ~6 months to reduce churn in UI.
    cleanup_device_names(days=180)
    active_out: List[Dict[str, Any]] = []
    active_ips = {d.get("ip") for d in active if d.get("ip")}
    ts_names = _tailscale_names()
    for d in active:
        ip = d.get("ip", "")
        override = overrides.get(ip, "").upper() if ip else ""
        mac = d.get("mac", "")
        is_ts = ip.startswith("100.64.")
        name = get_device_name(mac) if mac else ("" if is_ts else get_device_name_ip(ip))
        if mac:
            touch_device_seen(mac, name=name or None)
        elif ip and not is_ts:
            touch_device_seen_ip(ip, name=name or None)
        active_out.append(
            {
                **d,
                "mode": (override or global_mode),
                "override": override,
                "global_mode": global_mode,
                "name": name,
            }
        )

    # Add overrides not present in active leases (e.g., tailscale peers).
    for ip, mode in overrides.items():
        if ip in active_ips:
            continue
        name_ip = get_device_name_ip(ip) if ip else ""
        active_out.append(
            {
                "mac": "",
                "ip": ip,
                "hostname": ts_names.get(ip, ""),
                "expiry_epoch": 0,
                "mode": mode.upper(),
                "override": mode.upper(),
                "global_mode": global_mode,
                "name": name_ip,
                "source": "override",
            }
        )

    return {
        "active": active_out,
        "meta": {
            "leasefile": str(leasefile) if leasefile else "",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
