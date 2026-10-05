#!/usr/bin/env python3
"""Isolated attribution for inventory helpers; emits aggregate timings only.

This process never imports the FastAPI app or starts schedulers. It copies the
production SQLite snapshot through a read-only connection, redirects DB access
to that copy, and records SQL class/count/time without retaining SQL text.
"""
from __future__ import annotations

import json
import os
import ipaddress
import re
import sqlite3
import statistics
import subprocess
import tarfile
import tempfile
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path("/srv/fwrouter")
DB = Path("/var/lib/fwrouter-v2/fwrouter.db")
BASELINE = "24ef1ba"


def sql_class(sql: str) -> str:
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else "EMPTY"
    return head if head in {"SELECT", "WITH", "PRAGMA", "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP", "BEGIN", "COMMIT", "ROLLBACK"} else "OTHER"


class Recorder:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.seconds: dict[str, float] = defaultdict(float)
        self.max_seconds: dict[str, float] = defaultdict(float)

    def add(self, sql: str, seconds: float) -> None:
        cls = sql_class(sql)
        self.counts[cls] += 1
        self.seconds[cls] += seconds
        self.max_seconds[cls] = max(self.max_seconds[cls], seconds)

    def summary(self) -> dict[str, Any]:
        return {
            cls: {
                "count": count,
                "execute_fetch_ms_total": round(self.seconds[cls] * 1000, 3),
                "execute_fetch_ms_max": round(self.max_seconds[cls] * 1000, 3),
            }
            for cls, count in sorted(self.counts.items())
        }


REC = Recorder()


class TimedCursor:
    def __init__(self, cursor: sqlite3.Cursor, sql: str, started: float) -> None:
        self._cursor = cursor
        self._sql = sql
        self._started = started
        self._finished = False

    def _finish(self) -> None:
        if not self._finished:
            REC.add(self._sql, time.perf_counter() - self._started)
            self._finished = True

    def fetchall(self):
        try:
            return self._cursor.fetchall()
        finally:
            self._finish()

    def fetchone(self):
        try:
            return self._cursor.fetchone()
        finally:
            self._finish()

    def fetchmany(self, *args):
        try:
            return self._cursor.fetchmany(*args)
        finally:
            self._finish()

    def __iter__(self):
        try:
            yield from self._cursor
        finally:
            self._finish()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class TimedConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, *args):
        started = time.perf_counter()
        cursor = self._connection.execute(sql, *args)
        return TimedCursor(cursor, sql, started)

    def executemany(self, sql: str, *args):
        started = time.perf_counter()
        try:
            return self._connection.executemany(sql, *args)
        finally:
            REC.add(sql, time.perf_counter() - started)

    def executescript(self, sql: str):
        started = time.perf_counter()
        try:
            return self._connection.executescript(sql)
        finally:
            REC.add(sql, time.perf_counter() - started)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        if name == "_connection":
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)


def pstats(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "median_ms": round(statistics.median(values), 3),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fwrouter-attribution-") as tmp:
        snapshot = Path(tmp) / "fwrouter.db"
        source_uri = f"file:{DB}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=5)
        target = sqlite3.connect(snapshot)
        source.backup(target)
        target.close()
        source.close()

        import sys
        os.environ.pop("FWROUTER_STATE_DIR", None)
        os.environ.pop("STATE_DIR", None)
        archive = Path(tmp) / "source.tar"
        with archive.open("wb") as stream:
            subprocess.run(
                ["git", "-C", str(ROOT), "archive", "--format=tar", BASELINE],
                check=True, stdout=stream, stderr=subprocess.DEVNULL,
            )
        source_tree = Path(tmp) / "source"
        source_tree.mkdir()
        with tarfile.open(archive) as bundle:
            # This archive is produced locally by `git archive` from a pinned
            # repository commit, not supplied by an external source.
            bundle.extractall(source_tree)
        sys.path.insert(0, str(source_tree / "backend"))
        from fwrouter_api.db import connection as db_connection
        from fwrouter_api.services import live_probe_cache

        original_connect = db_connection.connect

        def isolated_connect():
            # Reuse project PRAGMAs and file permissions against the private snapshot.
            original_path = db_connection.get_db_path
            db_connection.get_db_path = lambda: snapshot
            try:
                connection = original_connect()
            finally:
                db_connection.get_db_path = original_path
            return TimedConnection(connection)

        db_connection.get_db_path = lambda: snapshot
        db_connection.connect = isolated_connect

        from fwrouter_api.services import ui_state_inventory
        from fwrouter_api.services import ui_state_common
        from fwrouter_api.services import state_projection
        from fwrouter_api.services import dataplane_status
        from fwrouter_api.services import servers
        from fwrouter_api.services import core_bypass
        from fwrouter_api.services import external_source_observations
        from fwrouter_api.services import ui_state_summary, custom_servers, server_inventory
        from fwrouter_api.services import provider_admin_projection, provider_managed
        from fwrouter_api.services import xray_status, modules
        from fwrouter_api.services import traffic
        from fwrouter_api.services import dataplane_global, dataplane_live
        from fwrouter_api.adapters import mihomo as mihomo_adapter

        # Keep explicit names so output gives attribution without retaining arguments.
        timed_targets = {
            "inventory.display_settings": (ui_state_inventory, "get_ui_display_settings"),
            "inventory.health_cache": (ui_state_inventory, "_subject_health_by_subject_for_ui"),
            "inventory.traffic_maps": (ui_state_inventory, "_traffic_maps"),
            "inventory.subscription_map": (ui_state_inventory, "_subscription_client_map"),
            "inventory.routing_state": (ui_state_inventory, "get_routing_global_state"),
            "health.projection": (state_projection, "build_subject_state_projection"),
            "health.subject_list": (state_projection, "list_subjects"),
            "health.runtime_enforcement": (state_projection, "build_runtime_enforcement_state"),
            "health.live_dataplane": (state_projection, "read_live_dataplane_payload"),
            "health.core_bypass": (state_projection, "get_core_bypass_state"),
            "health.routing_state": (state_projection, "_read_routing_global_state_readonly"),
            "health.adapter_snapshot": (state_projection, "_safe_health"),
            "runtime.dataplane_build": (dataplane_status, "build_runtime_enforcement_state"),
            "runtime.dataplane_uncached": (dataplane_status, "_build_runtime_enforcement_state_uncached"),
            "runtime.live_payload_cached": (dataplane_status, "read_live_dataplane_payload"),
            "runtime.live_payload_uncached": (dataplane_status, "_read_live_dataplane_payload"),
            "runtime.nft_counters": (dataplane_status, "inspect_transparent_path_counters"),
            "runtime.nft_marker_check": (dataplane_status, "applied_nft_markers_match_live"),
            "runtime.global_preflight": (dataplane_status, "build_global_preflight"),
            "runtime.mihomo_health": (dataplane_global, "_mihomo_health"),
            "runtime.applied_manifest": (dataplane_status, "read_applied_manifest"),
            "runtime.live_global_mode": (dataplane_status, "probe_live_global_mode"),
            "runtime.mihomo_http_health": (type(mihomo_adapter.DEFAULT_MIHOMO_ADAPTER), "health"),
            "runtime.script_runner": (type(dataplane_status.DEFAULT_SCRIPT_RUNNER), "run"),
            "runtime.nft_table": (dataplane_live, "_read_live_table"),
            "runtime.nft_mode_probe": (dataplane_live, "_probe_live_global_mode_uncached"),
            "runtime.core_bypass": (core_bypass, "get_core_bypass_state"),
            "runtime.routing_state": (servers, "get_routing_global_state"),
            "external.observation_cache": (state_projection, "cached_external_source_observations"),
            "workspace.builder": (ui_state_summary, "_build_ui_settings_workspace"),
            "workspace.counts": (ui_state_summary, "_ui_workspace_counts"),
            "workspace.system_counts": (ui_state_summary, "_system_subject_counts"),
            "workspace.modules": (ui_state_summary, "fetch_modules"),
            "workspace.subscription": (ui_state_summary, "get_subscription_state"),
            "workspace.provider_projection": (provider_managed, "provider_projection"),
            "workspace.xray_status": (xray_status, "get_xray_status"),
            "workspace.router_summary": (ui_state_summary, "get_ui_router_summary"),
            "workspace.traffic_accounting": (ui_state_summary, "get_traffic_accounting_state"),
            "workspace.operational_logs": (ui_state_summary, "list_operational_logs"),
            "workspace.technical_logs": (ui_state_summary, "list_technical_logs"),
            "servers.list_api": (custom_servers, "list_servers_api"),
            "servers.inventory": (custom_servers, "list_servers"),
            "servers.db_inventory": (server_inventory, "list_servers"),
            "servers.provider_groups": (provider_admin_projection, "provider_members_by_logical_id"),
            "servers.runtime_topologies": (server_inventory, "get_runtime_logical_topologies"),
        }
        timing: dict[str, list[float]] = defaultdict(list)
        serialization: dict[str, list[float]] = defaultdict(list)
        log_inputs: list[dict[str, int]] = []
        subprocess_calls: dict[str, list[float]] = defaultdict(list)
        http_calls: dict[str, list[float]] = defaultdict(list)

        def wrap(module, name, label):
            original = getattr(module, name, None)
            if not callable(original):
                return

            def measured(*args, **kwargs):
                if label == "workspace.technical_logs":
                    from fwrouter_api.core.config import get_settings
                    log_dir = get_settings().paths.technical_log_dir
                    log_inputs.append({
                        "file_count": sum(1 for path in log_dir.glob("*.jsonl") if path.is_file()),
                        "bytes": sum(path.stat().st_size for path in log_dir.glob("*.jsonl") if path.is_file()),
                    })
                start = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    timing[label].append((time.perf_counter() - start) * 1000)

            setattr(module, name, measured)

        for label, (module, name) in timed_targets.items():
            wrap(module, name, label)

        original_subprocess_run = subprocess.run
        def measured_subprocess_run(command, *args, **kwargs):
            command_name = Path(str(command[0])).name if isinstance(command, (tuple, list)) and command else "other"
            tokens = [str(item) for item in command[1:]] if isinstance(command, (tuple, list)) else []
            safe_read = (
                command_name == "dataplane-check.sh"
                or (command_name == "nft" and "list" in tokens and "-f" not in tokens)
                or (command_name == "ip" and any(tokens[:1] == [name] for name in ("rule", "route")) and any(name in tokens for name in ("show", "list")))
                or (command_name == "iptables" and any(name in tokens for name in ("-S", "--list", "-L")))
            )
            if not safe_read:
                raise RuntimeError(f"blocked non-read-only or unapproved subprocess: {command_name}")
            started = time.perf_counter()
            try:
                return original_subprocess_run(command, *args, **kwargs)
            finally:
                subprocess_calls[command_name].append((time.perf_counter() - started) * 1000)
        subprocess.run = measured_subprocess_run

        import httpx
        original_http_request = httpx.Client.request
        def measured_http_request(client, method, url, *args, **kwargs):
            parsed = httpx.URL(url)
            host = parsed.host.lower()
            loopback = host == "localhost"
            try:
                loopback = loopback or ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
            route_allowed = (
                parsed.path in {"/version", "/connections", "/proxies"}
                or re.fullmatch(r"/proxies/[^/]+", parsed.path) is not None
            )
            if str(method).upper() != "GET" or not loopback or not route_allowed:
                raise RuntimeError("blocked non-GET, non-loopback, or non-metadata HTTP request")
            path = parsed.path
            endpoint = "version" if path.endswith("/version") else "connections" if path.endswith("/connections") else "proxies" if "/proxies" in path else "other"
            started = time.perf_counter()
            try:
                return original_http_request(client, method, url, *args, **kwargs)
            finally:
                http_calls[f"loopback.GET.{endpoint}"].append((time.perf_counter() - started) * 1000)
        httpx.Client.request = measured_http_request

        results: dict[str, Any] = {}
        live_probe_cache.clear_live_probe_cache()
        REC.reset()
        start = time.perf_counter()
        rows = ui_state_inventory.list_ui_settings_inventory(role="router_core", limit=1000, live_observations=True)
        cold_ms = (time.perf_counter() - start) * 1000
        serialize_start = time.perf_counter()
        inventory_bytes = len(json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode())
        inventory_serialization_ms = (time.perf_counter() - serialize_start) * 1000
        inventory_sql = REC.summary()
        REC.reset()
        start = time.perf_counter()
        warm_rows = ui_state_inventory.list_ui_settings_inventory(role="router_core", limit=1000, live_observations=True)
        warm_ms = (time.perf_counter() - start) * 1000
        results["inventory_router_core"] = {
            "cold_ms": round(cold_ms, 3), "warm_ms": round(warm_ms, 3),
            "items": len(rows), "payload_bytes": inventory_bytes,
            "json_serialization_ms": round(inventory_serialization_ms, 3),
            "cold_sql": inventory_sql, "warm_sql": REC.summary(),
        }

        # Isolate the cache-miss canonical Health loader and the dominant nested calls.
        live_probe_cache.clear_live_probe_cache()
        REC.reset()
        start = time.perf_counter()
        workspace = ui_state_summary.get_ui_settings_workspace()
        workspace_cold_ms = (time.perf_counter() - start) * 1000
        serialize_start = time.perf_counter()
        workspace_bytes = len(json.dumps(workspace, separators=(",", ":"), ensure_ascii=False).encode())
        workspace_serialization_ms = (time.perf_counter() - serialize_start) * 1000
        workspace_sql = REC.summary()
        REC.reset()
        start = time.perf_counter()
        ui_state_summary.get_ui_settings_workspace()
        workspace_warm_ms = (time.perf_counter() - start) * 1000
        results["workspace"] = {
            "cold_ms": round(workspace_cold_ms, 3), "warm_ms": round(workspace_warm_ms, 3),
            "json_serialization_ms": round(workspace_serialization_ms, 3),
            "json_payload_bytes": workspace_bytes, "cold_sql": workspace_sql,
        }

        live_probe_cache.clear_live_probe_cache()
        REC.reset()
        start = time.perf_counter()
        servers_result = custom_servers.list_servers_api(inventory_state="active", limit=1000)
        servers_cold_ms = (time.perf_counter() - start) * 1000
        serialize_start = time.perf_counter()
        servers_bytes = len(json.dumps(servers_result, separators=(",", ":"), ensure_ascii=False).encode())
        servers_serialization_ms = (time.perf_counter() - serialize_start) * 1000
        servers_sql = REC.summary()
        REC.reset()
        start = time.perf_counter()
        warm_servers = custom_servers.list_servers_api(inventory_state="active", limit=1000)
        servers_warm_ms = (time.perf_counter() - start) * 1000
        results["servers_active"] = {
            "cold_ms": round(servers_cold_ms, 3), "warm_ms": round(servers_warm_ms, 3),
            "json_serialization_ms": round(servers_serialization_ms, 3),
            "items": len(servers_result), "payload_bytes": servers_bytes,
            "cold_sql": servers_sql, "warm_items": len(warm_servers),
        }
        results["helper_times"] = {key: pstats(values) for key, values in sorted(timing.items()) if values}
        results["serialization"] = {key: pstats(values) for key, values in sorted(serialization.items()) if values}
        results["technical_log_inputs"] = log_inputs
        results["subprocess_calls"] = {name: pstats(values) for name, values in sorted(subprocess_calls.items())}
        results["http_controller_calls"] = {name: pstats(values) for name, values in sorted(http_calls.items())}
        results["snapshot"] = {"source_db_bytes": DB.stat().st_size, "snapshot_method": "sqlite backup from read-only URI", "source_commit": BASELINE}
        results["call_policy"] = "fail-closed: only GETs to loopback Mihomo metadata routes and approved read-only dataplane commands; provider/external hosts, non-GET routes, probes, app, and schedulers are blocked"
        print(json.dumps(results, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
