#!/usr/bin/env python3
"""Observe six tiny GETs around the externally coordinated API restart marker."""
from __future__ import annotations
import datetime as dt
import json
import math
import os
import pathlib
import re
import subprocess
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "api_restart_overlap.json"
MARKER = pathlib.Path("/run/fwrouter-v2/live-bench-xray/api-restart-begin")
URL = "https://www.gstatic.com/generate_204"


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def tcp_snapshot():
    out = {}
    try:
        lines = pathlib.Path("/proc/net/snmp").read_text().splitlines()
        for i, line in enumerate(lines[:-1]):
            if line.startswith("Tcp:") and lines[i+1].startswith("Tcp:"):
                raw = dict(zip(line.split()[1:], lines[i+1].split()[1:]))
                for k in ("ActiveOpens", "AttemptFails", "EstabResets", "OutRsts", "InErrs", "RetransSegs"):
                    if k in raw: out[k] = int(raw[k])
                break
    except OSError: pass
    return out


def api_process():
    try:
        raw = subprocess.run(["systemctl", "show", "fwrouter-api.service", "--property=ActiveState,MainPID", "--value"],
                             capture_output=True, text=True, timeout=2).stdout.splitlines()
        state, pid_text = (raw + ["", ""])[:2]
        pid = int(pid_text) if pid_text.isdigit() and int(pid_text) > 0 else 0
        rss_kb = None
        cpu_ticks = None
        if pid:
            status = pathlib.Path(f"/proc/{pid}/status").read_text()
            m = re.search(r"^VmRSS:\s+(\d+)\s+kB$", status, re.M)
            rss_kb = int(m.group(1)) if m else None
            stat = pathlib.Path(f"/proc/{pid}/stat").read_text().split()
            cpu_ticks = int(stat[13]) + int(stat[14])
        return {"active_state": state, "process_present": bool(pid), "rss_kb": rss_kb, "cpu_ticks": cpu_ticks}
    except Exception as exc:
        return {"read_error_class": type(exc).__name__}


def host_snapshot():
    result = {"api": api_process(), "tcp": tcp_snapshot()}
    try:
        result["mem_available_kb"] = int(next(x.split()[1] for x in pathlib.Path("/proc/meminfo").read_text().splitlines() if x.startswith("MemAvailable:")))
        result["loadavg"] = pathlib.Path("/proc/loadavg").read_text().split()[:3]
    except Exception: pass
    return result


def request(path):
    cmd = ["curl", "--silent", "--show-error", "--max-time", "2", "--connect-timeout", "1",
           "--output", "/dev/null", "--write-out",
           '{"dns_s":"%{time_namelookup}","connect_s":"%{time_connect}","appconnect_s":"%{time_appconnect}","ttfb_s":"%{time_starttransfer}","total_s":"%{time_total}","bytes":"%{size_download}","http_status":"%{http_code}"}']
    if path == "direct_wan": cmd += ["--interface", "enp1s0", "--noproxy", "*"]
    else: cmd += ["--proxy", "http://127.0.0.1:5201", "--noproxy", ""]
    cmd += [URL]
    start = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "NO_PROXY": "", "no_proxy": ""}, timeout=3)
    try: vals = json.loads(p.stdout)
    except Exception: vals = {}
    for key in ("dns_s", "connect_s", "appconnect_s", "ttfb_s", "total_s"):
        try: vals[key] = float(vals[key])
        except (KeyError, ValueError, TypeError): vals[key] = None
    try: vals["bytes"] = int(vals["bytes"])
    except (ValueError, TypeError): vals["bytes"] = None
    if p.returncode == 0:
        if vals.get("connect_s") is not None and vals.get("dns_s") is not None:
            vals["tcp_phase_s"] = max(0.0, vals["connect_s"] - vals["dns_s"])
        if vals.get("appconnect_s") is not None and vals.get("connect_s") is not None:
            vals["tls_or_proxy_phase_s"] = max(0.0, vals["appconnect_s"] - vals["connect_s"])
        if vals.get("ttfb_s") is not None and vals.get("appconnect_s") is not None:
            vals["request_to_firstbyte_s"] = max(0.0, vals["ttfb_s"] - vals["appconnect_s"])
    return {"path": path, "target": "www.gstatic.com", "curl_exit": p.returncode,
            "error_class": None if p.returncode == 0 else ("timeout" if p.returncode == 28 else f"curl_exit_{p.returncode}"),
            "http_status": vals.get("http_status"), "bytes": vals.get("bytes"),
            "dns_s": vals.get("dns_s"), "tcp_phase_s": vals.get("tcp_phase_s"),
            "tls_or_proxy_phase_s": vals.get("tls_or_proxy_phase_s"),
            "request_to_firstbyte_s": vals.get("request_to_firstbyte_s"), "total_s": vals.get("total_s"),
            "wall_s": round(time.monotonic() - start, 6)}


def pair(phase):
    before = host_snapshot()
    started_utc = utc_now()
    rows = [request("direct_wan"), request("mihomo_5201")]
    ended_utc = utc_now()
    after = host_snapshot()
    return {"phase": phase, "started_at_utc": started_utc, "finished_at_utc": ended_utc,
            "samples": rows, "host_before": before, "host_after": after,
            "tcp_global_delta": {k: after.get("tcp", {}).get(k, 0) - before.get("tcp", {}).get(k, 0) for k in before.get("tcp", {})}}


def marker_signature():
    try:
        s = MARKER.stat()
        return (s.st_ino, s.st_mtime_ns)
    except FileNotFoundError:
        return None


def main():
    started = utc_now()
    pre_marker = marker_signature()
    report = {"started_at_utc": started, "marker_path": str(MARKER), "marker_preexisting": pre_marker is not None,
              "target": "https://www.gstatic.com/generate_204", "samples": []}
    t0 = time.monotonic()
    report["samples"].append(pair("idle_baseline"))
    observed_marker = None
    deadline = time.monotonic() + 32
    event = threading.Event()
    while time.monotonic() < deadline:
        sig = marker_signature()
        if sig is not None and sig != pre_marker:
            observed_marker = {"signature_changed": True, "mtime_ns": sig[1], "observed_after_baseline_s": round(time.monotonic() - t0, 3)}
            break
        event.wait(min(.1, max(0.0, deadline - time.monotonic())))
    report["marker_observation"] = observed_marker
    if observed_marker is None:
        report["status"] = "marker_not_observed_within_32s"
    else:
        marker_observed_at = time.monotonic()
        for offset, name in ((10, "restart_plus_10s"), (20, "restart_plus_20s")):
            event.wait(max(0.0, marker_observed_at + offset - time.monotonic()))
            report["samples"].append(pair(name))
            if time.monotonic() - t0 > 70: break
        report["status"] = "complete" if len(report["samples"]) == 3 else "partial_deadline"
    report["finished_at_utc"] = utc_now()
    report["limitations"] = ["Three paired observations are not a percentile sample.",
                             "Host-wide TCP counters include unrelated traffic.",
                             "Mihomo proxy DNS timing is local-only; target DNS is upstream.",
                             "A failed observation would be classified by curl exit, not retried."]
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(OUT), "status": report["status"], "phases": [x["phase"] for x in report["samples"]],
                      "statuses": [[y.get("http_status") for y in x["samples"]] for x in report["samples"]]}, indent=2))


if __name__ == "__main__": main()
