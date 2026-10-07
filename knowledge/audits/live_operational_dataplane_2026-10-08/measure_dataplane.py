#!/usr/bin/env python3
"""Bounded, read-only FWRouter live data-plane measurement.

No body content, subject identifiers, credentials, or proxy bindings are written
to the result. Runtime JSON is generated next to this script when run.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import socket
import statistics
import subprocess
import threading
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "dataplane_measurements.json"
TARGETS = [
    "https://www.gstatic.com/generate_204",
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://example.com/",
]
N_SERIAL = 8
MAX_TIME = 12
MAX_RATE = "1M"
PROXY = "http://127.0.0.1:5201"
API = "http://127.0.0.1:5000/api/v2"
SUBJECT_LIST = API + "/subjects?subject_type=explicit_external_client&is_active=true&limit=500"
XSTATE = pathlib.Path("/var/lib/fwrouter-v2/xray/fwrouter-bindings.json")
CURL_FORMAT = "\n__METRICS__" + json.dumps({
    "dns_s": "%{time_namelookup}", "tcp_s": "%{time_connect}",
    "tls_s": "%{time_appconnect}", "ttfb_s": "%{time_starttransfer}",
    "total_s": "%{time_total}", "download_bytes": "%{size_download}",
    "download_Bps": "%{speed_download}", "http_status": "%{http_code}",
    "remote_ip": "%{remote_ip}", "num_connects": "%{num_connects}",
})
SAMPLE_LOCK = threading.Lock()
SAMPLES: list[dict] = []
STOP = False


def percentile(values: list[float], p: float):
    xs = sorted(values)
    if not xs:
        return None
    return xs[max(0, math.ceil(p * len(xs)) - 1)]


def proc_snapshot() -> dict:
    out: dict = {"mem_available_kb": None, "loadavg": None, "cpu_ticks": None, "rss_by_comm_kb": {}}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                out["mem_available_kb"] = int(line.split()[1])
                break
        out["loadavg"] = pathlib.Path("/proc/loadavg").read_text().split()[:3]
        cpu = pathlib.Path("/proc/stat").read_text().splitlines()[0].split()
        out["cpu_ticks"] = sum(map(int, cpu[1:]))
        by_comm: dict[str, int] = {}
        for p in pathlib.Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                comm = (p / "comm").read_text().strip()
                rss = int((p / "statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 1024
                if comm in {"mihomo", "xray", "curl"}:
                    by_comm[comm] = by_comm.get(comm, 0) + rss
            except (OSError, ValueError, IndexError):
                continue
        out["rss_by_comm_kb"] = by_comm
    except OSError:
        pass
    return out


def sampler():
    while not STOP:
        snap = proc_snapshot()
        with SAMPLE_LOCK:
            SAMPLES.append(snap)
        time.sleep(0.2)


def tcp_stats() -> dict:
    counters = {}
    try:
        lines = pathlib.Path("/proc/net/snmp").read_text().splitlines()
        for i, line in enumerate(lines[:-1]):
            if line.startswith("Tcp:") and lines[i + 1].startswith("Tcp:"):
                keys = line.split()[1:]
                vals = lines[i + 1].split()[1:]
                raw = dict(zip(keys, vals))
                for name in ("ActiveOpens", "PassiveOpens", "AttemptFails", "EstabResets", "OutRsts", "InErrs", "RetransSegs", "CurrEstab"):
                    if name in raw:
                        counters[name] = int(raw[name])
                break
    except OSError:
        pass
    return counters


def interface_stats() -> dict:
    result = {}
    for key in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
        try:
            result[key] = int(pathlib.Path("/sys/class/net/enp1s0/statistics", key).read_text())
        except OSError:
            continue
    return result


def route_snapshot():
    try:
        r = subprocess.run(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=2)
        line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        return {"exit_code": r.returncode, "route": re.sub(r"\s+uid\s+\d+", "", line)}
    except Exception as exc:
        return {"error": type(exc).__name__}


def run_curl(path: str, url: str, index: int, concurrent: bool = False) -> dict:
    args = ["curl", "--silent", "--show-error", "--max-time", str(MAX_TIME), "--connect-timeout", "5",
            "--limit-rate", MAX_RATE, "--output", "/dev/null", "--write-out", CURL_FORMAT]
    if path == "direct_wan":
        args += ["--interface", "enp1s0", "--noproxy", "*"]
    else:
        args += ["--proxy", PROXY, "--noproxy", ""]
    args += [url]
    start = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=MAX_TIME + 2, env={**os.environ, "NO_PROXY": "", "no_proxy": ""})
        elapsed = time.monotonic() - start
        stdout = proc.stdout
        marker = "__METRICS__"
        met = {}
        if marker in stdout:
            _, raw = stdout.rsplit(marker, 1)
            met = json.loads(raw.strip())
        # curl's diagnostic text can contain host/network details; retain only its class.
        error_class = None if proc.returncode == 0 else ("timeout" if proc.returncode == 28 else f"curl_exit_{proc.returncode}")
        values = {}
        for k, v in met.items():
            try:
                values[k] = int(v) if k in ("download_bytes", "http_status", "num_connects") else (float(v) if k.endswith("_s") or k.endswith("_Bps") else v)
            except (ValueError, TypeError):
                values[k] = None
        if proc.returncode == 0:
            pairs = (("tcp_phase_s", "tcp_s", "dns_s"),
                     ("tls_or_connect_phase_s", "tls_s", "tcp_s"),
                     ("request_to_firstbyte_s", "ttfb_s", "tls_s"),
                     ("body_transfer_s", "total_s", "ttfb_s"))
            for name, end, begin in pairs:
                if isinstance(values.get(end), (float, int)) and isinstance(values.get(begin), (float, int)):
                    values[name] = max(0.0, values[end] - values[begin])
        return {"path": path, "target": urllib.parse.urlsplit(url).hostname, "sample": index,
                "concurrent_pair": concurrent, "curl_exit": proc.returncode, "error_class": error_class,
                "wall_s": round(elapsed, 6), **values}
    except Exception as exc:
        return {"path": path, "target": urllib.parse.urlsplit(url).hostname, "sample": index,
                "concurrent_pair": concurrent, "curl_exit": None, "error_class": type(exc).__name__, "wall_s": time.monotonic() - start}


def fetch_json(url: str, timeout: float = 4) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def xray_candidates() -> list[dict]:
    """Discover existing applied Xray subjects; only hashed refs leave this function."""
    try:
        payload = fetch_json(SUBJECT_LIST)
        rows = (payload.get("data") or {}).get("subjects") or []
        bindings_doc = json.loads(XSTATE.read_text())
        binding_ids = {str(x.get("subject_id")): x for x in bindings_doc.get("bindings", [])
                       if isinstance(x, dict) and x.get("status") == "applied"}
    except Exception as exc:
        return [{"discovery_error": type(exc).__name__}]
    candidates = {"global_auto": [], "fixed": []}
    for row in rows:
        if row.get("implementation_kind") != "xray" or not row.get("is_active"):
            continue
        sid = str(row.get("subject_id") or "")
        binding = binding_ids.get(sid)
        if not sid or not binding:
            continue
        effective = row.get("effective_state") or {}
        if effective.get("effective_mode") not in {"forced_vpn", "vpn", "enabled"}:
            continue
        cls = "global_auto" if effective.get("vpn_target_id") == "vpn-global" else "fixed"
        candidates[cls].append({"subject_id": sid, "subject_ref": hashlib.sha256(sid.encode()).hexdigest()[:12], "target_class": cls})
    return [x[0] for x in candidates.values() if x]


def xray_probe(candidate: dict) -> dict:
    sid = candidate["subject_id"]
    url = API + "/subjects/" + urllib.parse.quote(sid, safe="") + "/proxy-get-check"
    body = json.dumps({"url": TARGETS[0], "timeout_ms": 8000}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.load(response)
            code = response.status
        result = (data.get("data") or {}).get("proxy_get") or {}
        return {"subject_ref": candidate["subject_ref"], "target_class": candidate["target_class"],
                "http_api_status": code, "status": result.get("status"), "error_code": result.get("error_code"),
                "http_status": result.get("http_status"), "latency_ms": result.get("latency_ms"),
                "wall_s": round(time.monotonic() - started, 6),
                "effective_mode": result.get("mode"), "runtime_target_class": "global_auto" if result.get("effective_runtime_target") in {"vpn-global", "vpn-auto"} else "fixed_or_unreported"}
    except Exception as exc:
        return {"subject_ref": candidate["subject_ref"], "target_class": candidate["target_class"],
                "status": "request_failed", "error_code": type(exc).__name__, "wall_s": round(time.monotonic() - started, 6)}


def summarize(samples: list[dict]) -> dict:
    metrics = ("dns_s", "tcp_phase_s", "tls_or_connect_phase_s", "request_to_firstbyte_s", "body_transfer_s", "ttfb_s", "total_s", "wall_s")
    out = {"samples": len(samples), "successes": sum(1 for s in samples if s.get("curl_exit") == 0),
           "errors": sum(1 for s in samples if s.get("curl_exit") != 0), "metrics": {}}
    for name in metrics:
        xs = [float(s[name]) for s in samples if isinstance(s.get(name), (int, float)) and s.get("curl_exit") == 0]
        if xs:
            out["metrics"][name] = {"min": min(xs), "p50": percentile(xs, .5), "p95": percentile(xs, .95), "p99": percentile(xs, .99), "max": max(xs)}
    out["download_bytes_total"] = sum(int(s.get("download_bytes") or 0) for s in samples)
    out["reported_download_Bps_median"] = statistics.median([s["download_Bps"] for s in samples if isinstance(s.get("download_Bps"), (int, float))]) if samples else None
    return out


def main():
    global STOP
    started = dt.datetime.now(dt.timezone.utc)
    start_snap, tcp_before, nic_before = proc_snapshot(), tcp_stats(), interface_stats()
    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()
    samples: list[dict] = []
    targets = [TARGETS[i % len(TARGETS)] for i in range(N_SERIAL)]
    for path in ("direct_wan", "mihomo_5201"):
        for i, url in enumerate(targets, 1):
            samples.append(run_curl(path, url, i))
        # One bounded two-request wave per path; no overlapping paths.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_curl, path, TARGETS[i], i + 1, True) for i in range(2)]
            samples.extend(f.result() for f in futures)
    candidates = xray_candidates()
    xray_results = []
    for candidate in candidates[:2]:
        if "subject_id" in candidate:
            xray_results.append(xray_probe(candidate))
        else:
            xray_results.append(candidate)
    STOP = True
    sampler_thread.join(timeout=2)
    end_snap, tcp_after, nic_after = proc_snapshot(), tcp_stats(), interface_stats()
    with SAMPLE_LOCK:
        sampler_rows = list(SAMPLES)
    summaries = {path: summarize([s for s in samples if s["path"] == path and not s["concurrent_pair"]]) for path in ("direct_wan", "mihomo_5201")}
    per_target = {path: {host: summarize([s for s in samples if s["path"] == path and not s["concurrent_pair"] and s.get("target") == host])
                         for host in sorted({s["target"] for s in samples if s["path"] == path and not s["concurrent_pair"]})}
                  for path in ("direct_wan", "mihomo_5201")}
    concurrent_summary = {path: summarize([s for s in samples if s["path"] == path and s["concurrent_pair"]]) for path in ("direct_wan", "mihomo_5201")}
    aggregate_bytes = sum(s.get("download_bytes", 0) or 0 for s in samples)
    report = {
        "schema_version": 1, "started_at_utc": started.isoformat(), "finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_baseline": "6ba7f6f003ef67cbe350bea1e2d092e34c3eb660",
        "scope": {"paths": ["direct_wan(enp1s0,noproxy)", "explicit_mihomo_mixed_proxy(127.0.0.1:5201); outbound chain not yet attributed"], "serial_per_path": N_SERIAL,
                  "serial_target_order": [urllib.parse.urlsplit(u).hostname for u in targets], "concurrency_max": 2,
                  "rate_limit_per_curl": MAX_RATE, "transfer_payload_bytes_observed": aggregate_bytes,
                  "payload_budget_bytes": 10_000_000, "no_body_content_saved": True, "proxy_environment_overridden": True},
        "topology": {"direct_route_snapshot": route_snapshot(), "mihomo_proxy": PROXY, "interface": "enp1s0"},
        "serial_summary": summaries, "serial_summary_per_target": per_target,
        "bounded_concurrency_summary": concurrent_summary, "samples": samples,
        "xray_subject_proxy_get": {"method": "existing active Xray projection joined to applied binding; max one per route class", "results": xray_results,
                                   "subject_count_by_projected_class": {"global_auto": None, "fixed": None},
                                   "probe_scope": "server-local exact subject handoff GET; not remote client ingress"},
        "resources": {"snapshot_start": start_snap, "snapshot_end": end_snap,
                      "sample_count": len(sampler_rows), "sample_interval_s": 0.2,
                      "peak_rss_by_comm_kb": {n: max((s.get("rss_by_comm_kb", {}).get(n, 0) for s in sampler_rows), default=0) for n in ("mihomo", "xray", "curl")},
                      "mem_available_min_kb": min((s["mem_available_kb"] for s in sampler_rows if s.get("mem_available_kb") is not None), default=None),
                      "tcp_counters_before": tcp_before, "tcp_counters_after": tcp_after,
                      "tcp_counter_delta": {k: tcp_after.get(k, 0) - tcp_before.get(k, 0) for k in tcp_after},
                      "wan_interface_before": nic_before, "wan_interface_after": nic_after,
                      "wan_interface_delta": {k: nic_after.get(k, 0) - nic_before.get(k, 0) for k in nic_after}},
        "limitations": ["TCP host counters include unrelated system traffic during the window.",
                        "Server-local probes cannot establish client-to-router ingress performance.",
                        "Throughput is not inferred from small-response curl download rates; no bulk transfer test was run.",
                        "No UDP loss, sustained stream, or remote client Xray ingress was measured.",
                        "Curl DNS time on the HTTP proxy path measures local proxy-name resolution only; destination DNS is handled upstream and is not separately observable.",
                        "Proxy-path TCP/TLS timing is end-to-end through HTTP CONNECT and includes the upstream path."]
    }
    # Re-fetch projection only for safe class counts, never retain identifiers.
    try:
        rows = (fetch_json(SUBJECT_LIST).get("data") or {}).get("subjects") or []
        counts = {"global_auto": 0, "fixed": 0}
        for row in rows:
            e = row.get("effective_state") or {}
            if row.get("implementation_kind") == "xray" and row.get("is_active") and e.get("effective_mode") in {"forced_vpn", "vpn", "enabled"}:
                counts["global_auto" if e.get("vpn_target_id") == "vpn-global" else "fixed"] += 1
        report["xray_subject_proxy_get"]["subject_count_by_projected_class"] = counts
    except Exception:
        pass
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(OUT), "serial": summaries, "concurrency": concurrent_summary,
                      "aggregate_transfer_bytes": aggregate_bytes, "xray_probe_results": xray_results,
                      "resource_peak_rss_kb": report["resources"]["peak_rss_by_comm_kb"],
                      "tcp_counter_delta": report["resources"]["tcp_counter_delta"]}, indent=2))


if __name__ == "__main__":
    main()
