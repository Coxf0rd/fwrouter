#!/usr/bin/env python3
"""One bounded /servers handler attribution on a private SQLite backup.

Run from the project venv. Emits only aggregate SQL classes/timings, row counts,
encoded sizes, and loopback runtime GET timing. It never imports the FastAPI app
or starts schedulers, and fails closed on subprocess or non-inventory HTTP.
"""
from __future__ import annotations

import collections
import ipaddress
import json
import os
import pathlib
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any


DB = pathlib.Path("/var/lib/fwrouter-v2/fwrouter.db")
BACKEND = pathlib.Path("/srv/fwrouter/backend")
MAX_RUNTIME_GETS = 20


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fwrouter-stage4b-servers-") as temp:
        root = pathlib.Path(temp)
        snapshot = root / "fwrouter.db"
        source = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        target = sqlite3.connect(snapshot, timeout=5)
        try:
            source.backup(target, pages=512, sleep=0.01)
        finally:
            target.close()
            source.close()
        snapshot.chmod(0o600)

        for key in list(os.environ):
            if key.startswith("FWROUTER_") or key == "STATE_DIR":
                os.environ.pop(key, None)
        os.environ["FWROUTER_STATE_DIR"] = str(root / "isolated-state")
        sys.path.insert(0, str(BACKEND))

        from fwrouter_api.core.config import Settings, get_settings

        Settings.model_config["env_file"] = None
        get_settings.cache_clear()
        from fwrouter_api.db import connection as db_connection

        db_connection.get_db_path = lambda: snapshot
        raw_connect = db_connection.connect
        sql: dict[str, dict[str, float | int]] = collections.defaultdict(
            lambda: {"count": 0, "total_ms": 0.0, "rows": 0}
        )
        db_connect_ms: list[float] = []

        class TimedCursor:
            def __init__(self, cursor: sqlite3.Cursor, statement: str, started: float) -> None:
                self.cursor = cursor
                self.statement = statement
                self.started = started
                self.finished = False

            def finish(self, row_count: int = 0) -> None:
                if self.finished:
                    return
                kind = self.statement.lstrip().split(None, 1)[0].upper()
                entry = sql[kind]
                entry["count"] += 1
                entry["total_ms"] += (time.perf_counter() - self.started) * 1000
                entry["rows"] += row_count
                self.finished = True

            def fetchall(self):
                try:
                    rows = self.cursor.fetchall()
                    self.finish(len(rows))
                    return rows
                except Exception:
                    self.finish()
                    raise

            def fetchone(self):
                try:
                    row = self.cursor.fetchone()
                    self.finish(int(row is not None))
                    return row
                except Exception:
                    self.finish()
                    raise

            def __iter__(self):
                count = 0
                try:
                    for row in self.cursor:
                        count += 1
                        yield row
                finally:
                    self.finish(count)

            def __getattr__(self, name: str):
                return getattr(self.cursor, name)

        class TimedConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, statement: str, *args):
                started = time.perf_counter()
                try:
                    cursor = self.connection.execute(statement, *args)
                except Exception:
                    kind = statement.lstrip().split(None, 1)[0].upper()
                    entry = sql[kind]
                    entry["count"] += 1
                    entry["total_ms"] += (time.perf_counter() - started) * 1000
                    raise
                return TimedCursor(cursor, statement, started)

            def __getattr__(self, name: str):
                return getattr(self.connection, name)

        def timed_connect():
            started = time.perf_counter()
            connection = raw_connect()
            db_connect_ms.append((time.perf_counter() - started) * 1000)
            return TimedConnection(connection)

        db_connection.connect = timed_connect

        raw_getaddrinfo = socket.getaddrinfo

        def loopback_only_getaddrinfo(host: Any, *args, **kwargs):
            try:
                if not ipaddress.ip_address(str(host)).is_loopback:
                    raise RuntimeError("non-loopback DNS/network lookup blocked")
            except ValueError as exc:
                raise RuntimeError("hostname lookup blocked") from exc
            return raw_getaddrinfo(host, *args, **kwargs)

        socket.getaddrinfo = loopback_only_getaddrinfo

        import httpx

        runtime_gets: list[dict[str, Any]] = []
        raw_request = httpx.Client.request

        def bounded_runtime_get(client, method, url, *args, **kwargs):
            parsed = httpx.URL(url)
            try:
                loopback = ipaddress.ip_address(parsed.host).is_loopback
            except ValueError:
                loopback = False
            allowed_path = parsed.path == "/proxies" or bool(
                re.fullmatch(r"/proxies/[^/]+", parsed.path)
            ) and not parsed.path.endswith("/delay")
            if (
                str(method).upper() != "GET"
                or not loopback
                or parsed.port != 5200
                or not allowed_path
            ):
                raise RuntimeError("HTTP request outside bounded runtime inventory/state GET blocked")
            if len(runtime_gets) >= MAX_RUNTIME_GETS:
                raise RuntimeError("runtime GET cap exceeded")
            started = time.perf_counter()
            response = raw_request(client, method, url, *args, **kwargs)
            runtime_gets.append(
                {
                    "route": "proxies_inventory" if parsed.path == "/proxies" else "proxy_state",
                    "status": response.status_code,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "response_bytes": len(response.content),
                }
            )
            return response

        httpx.Client.request = bounded_runtime_get

        def block_subprocess(*args, **kwargs):
            raise RuntimeError("subprocess blocked during bounded /servers attribution")

        subprocess.run = block_subprocess
        subprocess.Popen = block_subprocess

        from fwrouter_api.services import (
            custom_servers,
            logical_topology,
            provider_admin_projection,
            server_inventory,
        )

        stages: dict[str, list[float]] = {}

        def timed(owner, name: str, label: str) -> None:
            original = getattr(owner, name)
            samples: list[float] = []

            def measured(*args, **kwargs):
                started = time.perf_counter()
                result = original(*args, **kwargs)
                samples.append((time.perf_counter() - started) * 1000)
                return result

            setattr(owner, name, measured)
            stages[label] = samples

        timed(server_inventory, "list_servers", "server_inventory.list_servers")
        custom_servers.list_servers = server_inventory.list_servers
        timed(server_inventory, "_row_to_server", "server_inventory.row_projection")
        timed(server_inventory, "get_runtime_logical_topologies", "runtime.logical_topologies")
        timed(logical_topology, "get_logical_topologies", "runtime.persisted_topologies")
        timed(
            provider_admin_projection,
            "provider_members_by_logical_id",
            "provider.local_group_projection",
        )
        timed(custom_servers, "enrich_server_with_custom_metadata", "custom_servers.row_enrichment")

        from fwrouter_api.routes.servers import list_servers_endpoint
        from fwrouter_api.routes import servers as servers_route

        timed(servers_route, "list_servers_api", "custom_servers.list_servers_api")

        handler_started = time.perf_counter()
        response = list_servers_endpoint(inventory_state="active", limit=1000)
        handler_ms = (time.perf_counter() - handler_started) * 1000
        encode_started = time.perf_counter()
        body = json.dumps(
            response.model_dump(mode="json"),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        encode_ms = (time.perf_counter() - encode_started) * 1000

        def summarize(values: list[float]) -> dict[str, float | int]:
            return {
                "count": len(values),
                "total_ms": round(sum(values), 3),
                "max_ms": round(max(values), 3) if values else 0,
            }

        output = {
            "source": "working tree",
            "route": "GET /api/v2/servers?inventory_state=active&limit=1000",
            "handler_ms": round(handler_ms, 3),
            "response_rows": len(response.data.get("servers", [])),
            "db_snapshot": {
                "method": "SQLite backup from mode=ro source",
                "bytes": snapshot.stat().st_size,
                "retained": False,
            },
            "db_connect": summarize(db_connect_ms),
            "sql_execute_plus_fetch": {
                kind: {
                    "count": int(item["count"]),
                    "total_ms": round(float(item["total_ms"]), 3),
                    "rows": int(item["rows"]),
                }
                for kind, item in sorted(sql.items())
            },
            "service_stages": {name: summarize(values) for name, values in stages.items()},
            "runtime_gets": runtime_gets,
            "json_response_model_dump_and_encode": {
                "ms": round(encode_ms, 3),
                "bytes": len(body),
            },
            "queue_ms": None,
            "provider_api_calls": 0,
            "external_probes": 0,
            "subprocesses": 0,
            "calls": 1,
            "raw_payloads_or_ids_retained": False,
        }
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
