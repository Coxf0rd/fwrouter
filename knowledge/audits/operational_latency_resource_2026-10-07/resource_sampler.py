"""Bounded Linux /proc sampler for isolated FWRouter operation benchmarks.

This helper samples only an explicitly supplied process tree and caller-listed
SQLite/WAL/temp paths. Host network counters are returned separately because
they include unrelated traffic. Samples are observations at a bounded interval,
not proof that shorter spikes did not occur.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Iterable


def _proc_stat(pid: int) -> tuple[int, int, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        # comm is parenthesized and can itself contain spaces/parentheses.
        tail = raw[raw.rfind(")") + 2 :].split()
        ppid = int(tail[1])
        ticks = int(tail[11]) + int(tail[12])
        return ppid, ticks, int(tail[19])
    except (OSError, ValueError, IndexError):
        return None


def _tree(root_pid: int) -> list[int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            item = _proc_stat(int(entry.name))
            if item:
                parents[int(entry.name)] = item[0]
    selected = {root_pid}
    changed = True
    while changed:
        old = len(selected)
        selected.update(pid for pid, ppid in parents.items() if ppid in selected)
        changed = len(selected) != old
    return sorted(selected)


def _process(pid: int) -> dict | None:
    parsed = _proc_stat(pid)
    if not parsed:
        return None
    ppid, ticks, start_ticks = parsed
    status: dict[str, int] = {}
    io: dict[str, int] = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"VmRSS", "VmHWM", "Threads", "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches"}:
                try:
                    status[key] = int(value.strip().split()[0])
                except (ValueError, IndexError):
                    pass
        for line in Path(f"/proc/{pid}/io").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw"}:
                io[key] = int(value.strip())
    except (OSError, ValueError):
        return None
    return {"pid": pid, "ppid": ppid, "start_ticks": start_ticks, "cpu_ticks": ticks,
            "rss_kib": status.get("VmRSS", 0), "hwm_kib": status.get("VmHWM", 0),
            "threads": status.get("Threads", 0),
            "voluntary_cs": status.get("voluntary_ctxt_switches", 0),
            "involuntary_cs": status.get("nonvoluntary_ctxt_switches", 0), **io}


def _host_net() -> dict[str, dict[str, int]]:
    result = {}
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            iface, _, rest = line.partition(":")
            fields = rest.split()
            if len(fields) >= 9:
                result[iface.strip()] = {"rx_bytes": int(fields[0]), "tx_bytes": int(fields[8])}
    except (OSError, ValueError):
        pass
    return result


def _host_pressure() -> dict:
    """Capture lightweight host pressure context; not attributable to an operation."""
    result: dict = {"loadavg": None, "runnable": None, "processes": None, "psi": {}}
    try:
        fields = Path("/proc/loadavg").read_text().split()
        result["loadavg"] = [float(v) for v in fields[:3]]
        result["runnable"], result["processes"] = [int(v) for v in fields[3].split("/")[:2]]
    except (OSError, ValueError, IndexError):
        pass
    for resource in ("cpu", "io", "memory"):
        try:
            rows = {}
            for line in Path(f"/proc/pressure/{resource}").read_text().splitlines():
                fields = line.split()
                values = dict(part.split("=", 1) for part in fields[1:] if "=" in part)
                rows[fields[0]] = {"avg10": float(values["avg10"]), "total_us": int(values["total"])}
            result["psi"][resource] = rows
        except (OSError, ValueError, IndexError, KeyError):
            continue
    return result


def _path_sizes(paths: Iterable[str | Path]) -> dict[str, int | None]:
    result = {}
    for raw in paths:
        path = Path(raw)
        try:
            result[str(path)] = path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        except OSError:
            result[str(path)] = None
    return result


def sample_operation(label: str, root_pid: int, *, sqlite_paths: Iterable[str | Path] = (),
                     temp_paths: Iterable[str | Path] = (), interval_ms: int = 25,
                     max_samples: int = 400) -> dict:
    """Sample one operation while it runs; call `.finish()` after the operation.

    Usage: sampler=OperationSampler(...); run(); result=sampler.finish().
    CPU percent is normalized to one core (100% means one fully occupied core).
    Disk bytes are process-accounted /proc counters; physical SSD writes are not
    inferred from them. RSS is a sampled peak; VmHWM remains a process-lifetime
    counter and is never reported as an operation peak.
    """
    return OperationSampler(label, root_pid, sqlite_paths=sqlite_paths,
                            temp_paths=temp_paths, interval_ms=interval_ms,
                            max_samples=max_samples)


class OperationSampler:
    def __init__(self, label: str, root_pid: int, *, sqlite_paths=(), temp_paths=(), interval_ms=25, max_samples=400):
        self.label = label
        self.root_pid = root_pid
        self.sqlite_paths = tuple(sqlite_paths)
        self.temp_paths = tuple(temp_paths)
        self.interval = max(0.01, min(interval_ms, 250) / 1000)
        self.max_samples = max(2, min(max_samples, 1000))
        self.started = time.perf_counter()
        self.samples: list[dict] = []
        self.before = self._capture()
        self._sample_once()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fwrouter-audit-sampler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval) and len(self.samples) < self.max_samples:
            self._sample_once()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _cpu_pct(self, old: dict, new: dict) -> float:
        old_map = {(p["pid"], p["start_ticks"]): p for p in old["procs"]}
        ticks = sum(max(0, p["cpu_ticks"] - old_map.get((p["pid"], p["start_ticks"]), p)["cpu_ticks"])
                    for p in new["procs"])
        elapsed = max(0.001, new["time"] - old["time"])
        return ticks / os.sysconf("SC_CLK_TCK") / elapsed * 100

    def _capture(self) -> dict:
        pids = _tree(self.root_pid)
        items = [item for pid in pids if (item := _process(pid))]
        return {"time": time.perf_counter(), "procs": items, "host_net": _host_net(), "host_pressure": _host_pressure(),
                "sqlite_sizes": _path_sizes(self.sqlite_paths), "temp_sizes": _path_sizes(self.temp_paths)}

    def _sample_once(self) -> None:
        if len(self.samples) < self.max_samples:
            self.samples.append(self._capture())

    def finish(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sample_once()
        after = self._capture()
        start, end = self.before, after
        sampled_peak_rss = 0
        sampled_peak_procs = 0
        sampled_peak_threads = 0
        peak_cpu_percent = 0.0
        rows = [start] + self.samples + [end]
        previous = {(p["pid"], p["start_ticks"]): (p, rows[0]["time"]) for p in rows[0]["procs"]}
        cumulative = {key: {name: 0 for name in ("cpu_ticks", "rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw", "voluntary_cs", "involuntary_cs")}
                      for key in previous}
        interval_values = []
        for row in rows[1:]:
            current = {(p["pid"], p["start_ticks"]): p for p in row["procs"]}
            sampled_peak_rss = max(sampled_peak_rss, sum(p["rss_kib"] for p in current.values()))
            sampled_peak_procs = max(sampled_peak_procs, len(current))
            sampled_peak_threads = max(sampled_peak_threads, sum(p["threads"] for p in current.values()))
            for key, p in current.items():
                previous_item = previous.get(key)
                prior = previous_item[0] if previous_item else None
                totals = cumulative.setdefault(key, {name: 0 for name in ("cpu_ticks", "rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw", "voluntary_cs", "involuntary_cs")})
                for name in totals:
                    value = p.get(name, 0)
                    old_value = prior.get(name, 0) if prior else 0
                    totals[name] += max(0, value - old_value)
                if prior is not None:
                    elapsed = max(0.001, row["time"] - previous_item[1])
                    cpu_pct = max(0, p["cpu_ticks"] - prior["cpu_ticks"]) / os.sysconf("SC_CLK_TCK") / elapsed * 100
                    interval_values.append(elapsed)
                    peak_cpu_percent = max(peak_cpu_percent, cpu_pct)
                previous[key] = (p, row["time"])
        cpu_ticks = sum(item["cpu_ticks"] for item in cumulative.values())
        io_totals = {name: sum(item[name] for item in cumulative.values()) for name in ("rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw")}
        duration = max(0.0, end["time"] - self.started)
        start_files = start["sqlite_sizes"]
        end_files = end["sqlite_sizes"]
        sqlite_peak = {}
        for key in start_files:
            vals = [row["sqlite_sizes"].get(key) for row in rows]
            sqlite_peak[key] = max((v for v in vals if v is not None), default=None)
        temp_peak = {}
        for key in start["temp_sizes"]:
            vals = [row["temp_sizes"].get(key) for row in rows]
            temp_peak[key] = max((v for v in vals if v is not None), default=None)
        return {
            "label": self.label, "duration_ms": round(duration * 1000, 3),
            "sample_interval_ms": round(self.interval * 1000, 1), "sample_count": len(self.samples),
            "observed_interval_min_ms": round(min(interval_values) * 1000, 3) if interval_values else None,
            "observed_interval_max_ms": round(max(interval_values) * 1000, 3) if interval_values else None,
            "sampled_peak_cpu_one_core_pct": round(peak_cpu_percent, 2),
            "process_cpu_ms": round(cpu_ticks / os.sysconf("SC_CLK_TCK") * 1000, 3),
            "sampled_peak_tree_rss_kib": sampled_peak_rss,
            "tree_rss_at_start_kib": sum(p["rss_kib"] for p in start["procs"]),
            "sampled_peak_tree_rss_delta_kib": max(0, sampled_peak_rss - sum(p["rss_kib"] for p in start["procs"])),
            "process_lifetime_hwm_kib_sum_before": sum(p["hwm_kib"] for p in start["procs"]),
            "process_lifetime_hwm_kib_sum_after": sum(p["hwm_kib"] for p in end["procs"]),
            "process_lifetime_hwm_is_not_operation_peak": True,
            "sampled_peak_processes": sampled_peak_procs, "sampled_peak_threads": sampled_peak_threads,
            "process_io_delta": io_totals,
            "context_switch_delta_proc_status": {
                "voluntary": sum(item["voluntary_cs"] for item in cumulative.values()),
                "involuntary": sum(item["involuntary_cs"] for item in cumulative.values())},
            "process_io_scope_note": "sampled interval deltas accumulated for observed process identities; a child shorter than the sampling interval may be missed",
            "sqlite_file_sizes_before": start_files, "sqlite_file_sizes_after": end_files,
            "sqlite_file_sizes_sampled_peak": sqlite_peak,
            "temp_file_sizes_before": start["temp_sizes"], "temp_file_sizes_after": end["temp_sizes"],
            "temp_file_sizes_sampled_peak": temp_peak,
            "host_network_bytes_before": start["host_net"], "host_network_bytes_after": end["host_net"],
            "host_pressure_before": start["host_pressure"], "host_pressure_after": end["host_pressure"],
            "host_pressure_sampled_peak": {
                "max_loadavg_1m": max((row["host_pressure"]["loadavg"][0] for row in rows if row["host_pressure"]["loadavg"]), default=None),
                "max_runnable": max((row["host_pressure"]["runnable"] for row in rows if row["host_pressure"]["runnable"] is not None), default=None),
                "max_psi_avg10": {
                    resource: max((row["host_pressure"].get("psi", {}).get(resource, {}).get("some", {}).get("avg10", 0.0) for row in rows), default=None)
                    for resource in ("cpu", "io", "memory")
                },
            },
            "host_pressure_scope_note": "loadavg/runqueue/PSI are host-wide context, not attributable to this operation",
            "sampling_limit_note": "25ms sampled process-tree peaks; shorter spikes may be missed; host network and physical SSD are not attributable",
        }


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Bounded process-tree resource sampler")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--label", default="read-only-live-bracket")
    parser.add_argument("--sqlite", action="append", default=[])
    parser.add_argument("--temp", action="append", default=[])
    args = parser.parse_args()
    sampler = sample_operation(args.label, args.pid, sqlite_paths=args.sqlite, temp_paths=args.temp)
    time.sleep(max(0.05, min(args.duration, 30.0)))
    print(json.dumps(sampler.finish(), sort_keys=True))
