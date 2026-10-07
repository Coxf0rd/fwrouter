#!/usr/bin/env python3
"""Very small capped transfer with read-only Mihomo connection attribution."""
from __future__ import annotations
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import threading
import time
import urllib.request
import yaml

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = pathlib.Path("/var/lib/fwrouter-v2/generated/mihomo/config.yaml")
URL = "https://speed.cloudflare.com/__down?bytes=262144"
RATE = "128K"
STOP = False
CONNECTION_SAMPLES = []
PIDS = []
OBSERVER_STATS = {"calls": 0, "request_wall_s": 0.0}


def controller_secret():
    # Secret remains in process memory and is never printed or written.
    doc = yaml.safe_load(CONFIG.read_text())
    return str(doc.get("secret") or "")


def source_ports_for_pids(pids):
    inodes = set()
    for pid in pids:
        try:
            for fd in pathlib.Path(f"/proc/{pid}/fd").iterdir():
                match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(fd))
                if match:
                    inodes.add(match.group(1))
        except OSError:
            continue
    ports = set()
    try:
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            for row in pathlib.Path(table).read_text().splitlines()[1:]:
                fields = row.split()
                if len(fields) > 9 and fields[9] in inodes:
                    remote_port = int(fields[2].rsplit(":", 1)[1], 16)
                    if remote_port == 5201:
                        ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    except OSError:
        pass
    return ports


def controller_current_global_member(secret: str):
    req = urllib.request.Request("http://127.0.0.1:5200/proxies/vpn-global", headers={"Authorization": "Bearer " + secret})
    with urllib.request.urlopen(req, timeout=1) as r:
        return str((json.load(r).get("now") or ""))


def connection_snapshot(secret: str, source_ports, current_member):
    req = urllib.request.Request("http://127.0.0.1:5200/connections", headers={"Authorization": "Bearer " + secret})
    with urllib.request.urlopen(req, timeout=1) as r:
        payload = json.load(r)
    matches = []
    for c in payload.get("connections", []):
        m = c.get("metadata") or {}
        try:
            source_port = int(m.get("sourcePort") or 0)
        except (TypeError, ValueError):
            source_port = 0
        if str(m.get("host") or "").lower() != "speed.cloudflare.com" or source_port not in source_ports:
            continue
        chain = [str(x) for x in c.get("chains", [])]
        matches.append({
            "chain_hops": len(chain),
            "has_direct_hop": "DIRECT" in chain,
            "chain_fingerprint": hashlib.sha256("\0".join(chain).encode()).hexdigest()[:12],
            "rule_fingerprint": hashlib.sha256(str(c.get("rule") or "").encode()).hexdigest()[:12],
            "is_direct": "DIRECT" in chain,
            "chain_includes_vpn_global_group": "vpn-global" in chain,
            "chain_includes_current_global_member": bool(current_member and current_member in chain),
            "current_member_fingerprint": hashlib.sha256(current_member.encode()).hexdigest()[:12] if current_member else None,
            "upload_bytes": int(c.get("upload", 0) or 0), "download_bytes": int(c.get("download", 0) or 0),
        })
    return {"connection_count": len(matches), "connections": matches}


def sampler(secret):
    try:
        current_member = controller_current_global_member(secret)
        OBSERVER_STATS["vpn_global_group_read"] = True
    except Exception as exc:
        current_member = ""
        OBSERVER_STATS["vpn_global_group_read"] = False
        OBSERVER_STATS["vpn_global_group_error"] = type(exc).__name__
    while not STOP:
        started = time.monotonic()
        try:
            ports = source_ports_for_pids(PIDS)
            if ports:
                sample = connection_snapshot(secret, ports, current_member)
                CONNECTION_SAMPLES.extend(sample["connections"])
                OBSERVER_STATS["calls"] += 1
                OBSERVER_STATS["request_wall_s"] += time.monotonic() - started
        except Exception as exc:
            CONNECTION_SAMPLES.append({"observer_error_class": type(exc).__name__})
        time.sleep(.2)


def one_transfer(path, i):
    cmd = ["curl", "--silent", "--show-error", "--max-time", "5", "--connect-timeout", "3", "--limit-rate", RATE,
           "--max-filesize", "262144", "--output", "/dev/null", "--write-out",
           '{"http_status":"%{http_code}","total_s":"%{time_total}","dns_s":"%{time_namelookup}","tcp_s":"%{time_connect}","tls_s":"%{time_appconnect}","bytes":"%{size_download}","rate_Bps":"%{speed_download}"}', URL]
    if path == "direct_wan":
        cmd[1:1] = ["--interface", "enp1s0", "--noproxy", "*"]
    else:
        cmd[1:1] = ["--proxy", "http://127.0.0.1:5201", "--noproxy", ""]
    env = {**os.environ, "NO_PROXY": "", "no_proxy": ""}
    start = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    PIDS.append(p.pid)
    stdout, stderr = p.communicate(timeout=7)
    try:
        result = json.loads(stdout)
    except Exception:
        result = {}
    result.update({"path": path, "flow": i, "curl_exit": p.returncode,
                   "error_class": None if p.returncode == 0 else ("timeout" if p.returncode == 28 else f"curl_exit_{p.returncode}"),
                   "wall_s": round(time.monotonic() - start, 5)})
    return result


def ping_direct():
    p = subprocess.run(["ping", "-n", "-I", "enp1s0", "-c", "5", "-W", "1", "1.1.1.1"], capture_output=True, text=True, timeout=8)
    import re
    loss = re.search(r"([0-9.]+)% packet loss", p.stdout)
    rtt = re.search(r"rtt min/avg/max/mdev = ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms", p.stdout)
    return {"exit_code": p.returncode, "packet_loss_percent": float(loss.group(1)) if loss else None,
            "rtt_min_avg_max_mdev_ms": [float(x) for x in rtt.groups()] if rtt else None,
            "attempted": 5, "interface": "enp1s0", "target": "1.1.1.1"}


def run_path(path, secret):
    global STOP
    STOP = False
    CONNECTION_SAMPLES.clear()
    PIDS.clear()
    OBSERVER_STATS.clear()
    OBSERVER_STATS.update({"calls": 0, "request_wall_s": 0.0})
    cpu_before = time.process_time()
    try:
        if path == "mihomo_5201":
            t = threading.Thread(target=sampler, args=(secret,), daemon=True)
            t.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result() for f in [pool.submit(one_transfer, path, 1), pool.submit(one_transfer, path, 2)]]
    finally:
        STOP = True
        if path == "mihomo_5201":
            t.join(timeout=2)
    return {"transfers": results, "configured_cap_bytes": 262144, "rate_limit_per_flow": RATE,
            "aggregate_max_bytes": 524288, "controller_poll_interval_s": .2,
            "matching_connections": list(CONNECTION_SAMPLES),
            "controller_auth_or_observer_errors": sum(1 for s in CONNECTION_SAMPLES if "observer_error_class" in s),
            "controller_calls": OBSERVER_STATS["calls"], "controller_request_wall_s_total": round(OBSERVER_STATS["request_wall_s"], 6),
            "observer_and_runner_process_cpu_s": round(time.process_time() - cpu_before, 6),
            "vpn_global_group_read": OBSERVER_STATS.get("vpn_global_group_read"),
            "vpn_global_group_error": OBSERVER_STATS.get("vpn_global_group_error"),
            "observer_sampling_interval_s": .2}


def main():
    secret = controller_secret()
    if not secret:
        raise SystemExit("Mihomo controller secret unavailable; no probes run")
    before = dt.datetime.now(dt.timezone.utc).isoformat()
    report = {"started_at_utc": before, "target": "speed.cloudflare.com bounded 262144-byte object",
              "paths": {}}
    report["icmp_direct"] = ping_direct()
    for path in ("direct_wan", "mihomo_5201"):
        report["paths"][path] = run_path(path, secret)
    report["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["limitations"] = ["Transfer is rate capped and too small to represent path capacity.",
                             "Mihomo controller snapshots are sampled during flows; connections shorter than a poll interval may be missed.",
                             "Host-local direct WAN client is not remote router ingress."]
    out = ROOT / "dataplane_attribution.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(out), "icmp": report["icmp_direct"],
                      "paths": {k: {"transfers": v["transfers"], "matched_connections": len(v["matching_connections"]), "controller_errors": v["controller_auth_or_observer_errors"]}
                                for k, v in report["paths"].items()}}, indent=2))


if __name__ == "__main__":
    main()
