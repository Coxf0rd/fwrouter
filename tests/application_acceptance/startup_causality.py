"""Bounded test-only operation counters for qualified startup recovery workers.

This module is deliberately independent of FWRouter imports so its contracts can
be exercised with stdlib fixtures. It is installed only by the synthetic
application-acceptance worker when startup recovery is explicitly enabled.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import copy
import re
from collections import Counter
from pathlib import Path
from typing import Any


PHASES = frozenset({"foundation", "routing", "selector", "intended", "scoped", "xray", "dns"})
MAX_GROUPS = 128
MAX_HASH_BYTES = 128 * 1024 * 1024
_RECEIPT = Path("/tmp/fwrouter-receipts/startup-causality-worker.json")


class StartupCausality:
    """Aggregate operation cost by coarse phase without retaining inputs."""

    def __init__(self, receipt: Path = _RECEIPT) -> None:
        self.receipt = receipt
        self.lock = threading.RLock()
        self.snapshot_lock = threading.Lock()
        self.phase = "foundation"
        self.phases: dict[str, dict[str, Any]] = {}
        self.cache = Counter()
        self.restorers: list[Any] = []
        self.originals: dict[str, Any] = {}
        self.originals_module: Any = None
        self.hash_bytes = 0
        self.hash_cpu = 0.0
        self.hash_wall = 0.0
        self.hash_samples = 0
        self.hash_truncated = False
        self.hash_lock = threading.Lock()
        self.digest_seen: dict[tuple[str, str], int] = {}
        self.digest_repeated_calls = 0
        self.digest_repeated_bytes = 0
        self.digest_phase_stats: dict[str, dict[str, int]] = {}
        self.statement_seen: dict[tuple[str, str], int] = {}
        self.statement_repeated_calls = 0
        self.statement_unique_templates = 0
        self.statement_phase_stats: dict[str, dict[str, int]] = {}
        self.active_ops = 0
        self.completed = False
        self.bookkeeping_wall = 0.0
        self.operation_observer_wall = 0.0
        self.operation_observer_cpu = 0.0
        self.snapshot_wall = 0.0
        self.snapshot_cpu = 0.0
        self.json_size_unit = "characters for str; bytes for bytes; UTF-8 bytes for sampled large str"
        self.snapshot_error: str | None = None
        self.enabled = False
        self.recording = False
        self.last_snapshot_at = 0.0

    def set_phase(self, phase: str) -> None:
        phase = phase if phase in PHASES else "foundation"
        with self.lock:
            self.phase = phase
            self._phase(phase)
        self.snapshot()

    def _phase(self, phase: str) -> dict[str, Any]:
        return self.phases.setdefault(phase, {
            "json": {}, "sqlite": {}, "boundary": {}, "active_ops": 0,
            "completed_ops": 0, "wall_seconds": 0.0, "thread_cpu_seconds": 0.0,
            "max_wall_seconds": 0.0,
        })

    def _group(self, family: str, label: str) -> tuple[dict[str, Any], str]:
        phase = self._phase(self.phase)
        groups = phase[family]
        key = label[:80]
        if key not in groups and len(groups) >= MAX_GROUPS - 1:
            key = "other_capped"
        row = groups.setdefault(key, {"calls": 0, "bytes": 0, "wall_seconds": 0.0,
                                      "thread_cpu_seconds": 0.0, "max_wall_seconds": 0.0})
        return row, key

    def operation(self, family: str, label: str, byte_count: Any, callable_: Any, *args: Any, **kwargs: Any) -> Any:
        if not self.recording:
            return callable_(*args, **kwargs)
        observer_wall_start, observer_cpu_start = time.perf_counter(), time.thread_time()
        phase_for_key = self.phase
        statement_digest = self._statement_fingerprint(
            label, args[0] if family == "sqlite" and args else None)
        statement_key = (phase_for_key, statement_digest) if statement_digest else None
        with self.lock:
            phase_name = self.phase
            phase = self._phase(phase_name)
            phase["active_ops"] += 1
            self.active_ops += 1
        call_wall_start, call_cpu_start = time.perf_counter(), time.thread_time()
        call_elapsed = call_cpu = 0.0
        call_finished = False
        try:
            result = callable_(*args, **kwargs)
            call_elapsed = time.perf_counter() - call_wall_start
            call_cpu = time.thread_time() - call_cpu_start
            call_finished = True
            if callable(byte_count):
                size_wall = time.perf_counter()
                byte_count = byte_count(result)
                self.bookkeeping_wall += time.perf_counter() - size_wall
            return result
        finally:
            if not call_finished:
                call_elapsed = time.perf_counter() - call_wall_start
                call_cpu = time.thread_time() - call_cpu_start
            bookkeeping_started = time.perf_counter()
            with self.lock:
                self.active_ops -= 1
                phase = self._phase(phase_name)
                phase["active_ops"] = max(0, phase["active_ops"] - 1)
                phase["completed_ops"] += 1
                phase["wall_seconds"] += call_elapsed
                phase["thread_cpu_seconds"] += call_cpu
                phase["max_wall_seconds"] = max(phase["max_wall_seconds"], call_elapsed)
                row, _ = self._group(family, label)
                row["calls"] = min(1_000_000, row["calls"] + 1)
                row["bytes"] = min(2**63 - 1, row["bytes"] + max(0, byte_count if type(byte_count) is int else 0))
                row["wall_seconds"] += call_elapsed
                row["thread_cpu_seconds"] += call_cpu
                row["max_wall_seconds"] = max(row["max_wall_seconds"], call_elapsed)
                if statement_key is not None:
                    count = self.statement_seen.get(statement_key, 0)
                    if count:
                        self.statement_repeated_calls += 1
                        stats = self.statement_phase_stats.setdefault(phase_name, {"repeated_calls": 0, "unique_templates": 0})
                        stats["repeated_calls"] += 1
                    else:
                        self.statement_unique_templates += 1
                        stats = self.statement_phase_stats.setdefault(phase_name, {"repeated_calls": 0, "unique_templates": 0})
                        stats["unique_templates"] += 1
                    if sum(1 for phase, _ in self.statement_seen if phase == phase_name) < MAX_GROUPS:
                        self.statement_seen[statement_key] = count + 1
                    else:
                        self.hash_truncated = True
                self.bookkeeping_wall += time.perf_counter() - bookkeeping_started
            self._maybe_snapshot()
            total_wall = time.perf_counter() - observer_wall_start
            total_cpu = time.thread_time() - observer_cpu_start
            self.operation_observer_wall += max(0.0, total_wall - call_elapsed)
            self.operation_observer_cpu += max(0.0, total_cpu - call_cpu)

    def observe_json(self, module: Any) -> None:
        self.originals_module = module
        originals = {name: getattr(module, name) for name in ("loads", "dumps", "load", "dump")}
        self.originals["json"] = originals
        depth = threading.local()
        for name, original in originals.items():
            def wrap(value: Any, *args: Any, _name=name, _original=original, **kwargs: Any) -> Any:
                if not self.recording:
                    return _original(value, *args, **kwargs)
                if getattr(depth, "active", False):
                    return _original(value, *args, **kwargs)
                depth.active = True
                input_size = len(value) if isinstance(value, (str, bytes, bytearray)) else 0
                sampled_encoded: bytes | None = None
                conversion_wall = conversion_cpu = 0.0
                remaining_hash_budget = MAX_HASH_BYTES - self.hash_bytes
                if (_name == "loads" and isinstance(value, str) and input_size >= 64 * 1024
                        and self.recording and input_size <= remaining_hash_budget // 4
                        and self.hash_samples < MAX_GROUPS):
                    conversion_wall, conversion_cpu = time.perf_counter(), time.thread_time()
                    sampled_encoded = value.encode("utf-8")
                    conversion_wall = time.perf_counter() - conversion_wall
                    conversion_cpu = time.thread_time() - conversion_cpu
                    input_size = len(sampled_encoded)
                    self.hash_wall += conversion_wall
                    self.hash_cpu += conversion_cpu
                elif (_name == "loads" and self.recording
                      and isinstance(value, (bytes, bytearray)) and len(value) >= 64 * 1024
                      and len(value) <= remaining_hash_budget and self.hash_samples < MAX_GROUPS):
                    sampled_encoded = value
                size: Any = input_size
                if _name in {"dumps", "dump"}:
                    size = lambda result: len(result) if isinstance(result, (str, bytes, bytearray)) else 0
                try:
                    result = self.operation("json", _name, size, _original, value, *args, **kwargs)
                    if (self.recording and _name == "loads" and input_size >= 64 * 1024
                            and isinstance(value, (str, bytes, bytearray))):
                        if sampled_encoded is None:
                            self.hash_truncated = True
                        else:
                            with self.hash_lock:
                                encoded = sampled_encoded
                                if self.hash_bytes + len(encoded) <= MAX_HASH_BYTES and self.hash_samples < MAX_GROUPS:
                                    hash_wall, hash_cpu = time.perf_counter(), time.thread_time()
                                    digest = hashlib.sha256(encoded).hexdigest()
                                    self.hash_wall += time.perf_counter() - hash_wall
                                    self.hash_cpu += time.thread_time() - hash_cpu
                                    self.hash_bytes += len(encoded)
                                    self.hash_samples += 1
                                    phase_name = self.phase
                                    digest_key = (phase_name, digest)
                                    with self.lock:
                                        count = self.digest_seen.get(digest_key, 0)
                                        stats = self.digest_phase_stats.setdefault(
                                            phase_name, {"repeated_calls": 0, "repeated_bytes": 0, "unique_count": 0})
                                        if count:
                                            self.digest_repeated_calls += 1
                                            self.digest_repeated_bytes += len(encoded)
                                            stats["repeated_calls"] += 1
                                            stats["repeated_bytes"] += len(encoded)
                                        if digest_key not in self.digest_seen and sum(
                                                1 for phase, _ in self.digest_seen if phase == phase_name) < MAX_GROUPS:
                                            self.digest_seen[digest_key] = count + 1
                                            stats["unique_count"] += 1
                                        elif digest_key in self.digest_seen:
                                            self.digest_seen[digest_key] = count + 1
                                        else:
                                            self.hash_truncated = True
                                else:
                                    self.hash_truncated = True
                    return result
                finally:
                    depth.active = False
            setattr(module, name, wrap)
        self.restorers.append(lambda: [setattr(module, key, value) for key, value in originals.items()])

    def observe_loaded_json_aliases(self) -> None:
        originals = self.originals.get("json", {})
        if not originals:
            return
        for module in tuple(__import__("sys").modules.values()):
            namespace = getattr(module, "__dict__", None)
            if (not isinstance(namespace, dict)
                    or not str(namespace.get("__name__", "")).startswith("fwrouter_api")):
                continue
            for name, value in tuple(namespace.items()):
                for function_name, original in originals.items():
                    # The json module's wrappers are installed separately; patch
                    # only aliases that still point at the original callable.
                    if value is original:
                        replacement = getattr(self.originals_module, function_name)
                        namespace[name] = replacement
                        self.restorers.append(lambda ns=namespace, n=name, v=original: ns.__setitem__(n, v))
                        break

    def observe_sqlite(self, sqlite_module: Any = sqlite3) -> None:
        original_connect = sqlite_module.connect
        owner = self

        class Cursor(sqlite_module.Cursor):
            def _run(self, method: Any, sql: Any, *args: Any) -> Any:
                verb = sql.lstrip().split(None, 1)[0].upper() if isinstance(sql, str) and sql.strip() else "OTHER"
                label = verb if verb in {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "REPLACE"} else "OTHER"
                before = self.connection.in_transaction
                result = owner.operation("sqlite", "statement." + label,
                                         len(sql) if isinstance(sql, (str, bytes)) else 0,
                                         method, sql, *args)
                after = self.connection.in_transaction
                if before != after:
                    with owner.lock:
                        owner.cache["transaction_statement_begin" if after else "transaction_statement_end"] += 1
                return result

            def execute(self, sql: Any, parameters: Any = (), /) -> Any:
                return self._run(super().execute, sql, parameters)

            def executemany(self, sql: Any, seq_of_parameters: Any, /) -> Any:
                return self._run(super().executemany, sql, seq_of_parameters)

            def executescript(self, sql_script: Any, /) -> Any:
                return self._run(super().executescript, sql_script)

            def fetchone(self) -> Any:
                return owner.operation("sqlite", "fetch.fetchone", 0, super().fetchone)

            def fetchmany(self, size: Any = None, /) -> Any:
                return owner.operation("sqlite", "fetch.fetchmany", 0,
                                       super().fetchmany) if size is None else owner.operation(
                                           "sqlite", "fetch.fetchmany", 0, super().fetchmany, size)

            def fetchall(self) -> Any:
                return owner.operation("sqlite", "fetch.fetchall", 0, super().fetchall)

        class Connection(sqlite_module.Connection):
            def cursor(self, factory: Any = Cursor) -> Any:
                if factory is not Cursor:
                    with owner.lock:
                        owner.cache["sqlite_custom_cursor_factory_unobserved"] += 1
                return super().cursor(factory)

            def execute(self, sql: Any, parameters: Any = (), /) -> Any:
                cursor = self.cursor()
                cursor.execute(sql, parameters)
                return cursor

            def executemany(self, sql: Any, seq_of_parameters: Any, /) -> Any:
                cursor = self.cursor()
                cursor.executemany(sql, seq_of_parameters)
                return cursor

            def executescript(self, sql_script: Any, /) -> Any:
                cursor = self.cursor()
                cursor.executescript(sql_script)
                return cursor

            def commit(self) -> None:
                before = self.in_transaction
                result = owner.operation("sqlite", "transaction.commit", 0, super().commit)
                if before != self.in_transaction:
                    with owner.lock:
                        owner.cache["transaction_commit_transition"] += 1
                return result

            def rollback(self) -> None:
                before = self.in_transaction
                result = owner.operation("sqlite", "transaction.rollback", 0, super().rollback)
                if before != self.in_transaction:
                    with owner.lock:
                        owner.cache["transaction_rollback_transition"] += 1
                return result

            def __enter__(self) -> Any:
                return super().__enter__()

            def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
                before = self.in_transaction
                wall, cpu = time.perf_counter(), time.thread_time()
                try:
                    return super().__exit__(exc_type, exc_value, traceback)
                finally:
                    if before != self.in_transaction:
                        with owner.lock:
                            owner.cache["transaction_context_exit_transition"] += 1
                            owner.cache["transaction_context_exit_rollback"] += int(exc_type is not None)
                            owner.cache["transaction_context_exit_commit"] += int(exc_type is None)
                            owner.cache["transaction_context_exit_wall_seconds"] += time.perf_counter() - wall
                            owner.cache["transaction_context_exit_thread_cpu_seconds"] += time.thread_time() - cpu

        def connect(*args: Any, **kwargs: Any) -> Any:
            if len(args) > 5 and "factory" in kwargs:
                return original_connect(*args, **kwargs)
            supplied_factory = kwargs.get("factory", args[5] if len(args) > 5 else None)
            if supplied_factory is not None:
                if owner.recording:
                    with owner.lock:
                        owner.cache["sqlite_custom_factory_unobserved"] += 1
                return original_connect(*args, **kwargs)
            if len(args) > 5:
                args = (*args[:5], Connection, *args[6:])
            else:
                kwargs["factory"] = Connection
            return owner.operation("boundary", "sqlite.connect", 0, original_connect, *args, **kwargs)

        sqlite_module.connect = connect
        self.restorers.append(lambda: setattr(sqlite_module, "connect", original_connect))

    @staticmethod
    def _statement_fingerprint(label: str, sql: Any) -> str | None:
        if not label.startswith("statement.") or not isinstance(sql, str):
            return None
        # This digest is process-local and never serialized. Normalize literals
        # before hashing so sensitive SQL values do not leave the caller frame.
        normalized = re.sub(r"'(?:''|[^'])*'|\b\d+(?:\.\d+)?\b", "?", sql)
        return hashlib.blake2s(normalized.encode("utf-8", "replace"), digest_size=12).hexdigest()

    def observe_cache(self, cache_module: Any) -> None:
        original_get = cache_module.get_live_probe_cache
        original_clear = cache_module.clear_live_probe_cache
        owner = self

        def get_cache(key: str, **kwargs: Any) -> Any:
            if not owner.recording:
                return original_get(key, **kwargs)
            now = cache_module.monotonic()
            with cache_module._CACHE_LOCK:
                entry = cache_module._CACHE.get(key)
                in_flight = key in cache_module._INFLIGHT
                hit = isinstance(entry, tuple) and len(entry) == 3 and entry[0] > now
            label = "direct_hit" if hit and not kwargs.get("force_refresh") else (
                "coalesced_or_race" if in_flight else "loader_or_ambiguous")
            loader = kwargs.get("loader")
            counted_kwargs = dict(kwargs)
            loader_calls = {"calls": 0, "wall": 0.0, "cpu": 0.0, "size": None, "type": None}
            if callable(loader):
                def measured_loader(*args: Any, **loader_kwargs: Any) -> Any:
                    loader_started, loader_cpu = time.perf_counter(), time.thread_time()
                    try:
                        value = loader(*args, **loader_kwargs)
                        loader_calls["size"] = len(value) if isinstance(value, (dict, list, tuple, str, bytes, bytearray)) else None
                        loader_calls["type"] = type(value).__name__[:32]
                        return value
                    finally:
                        loader_calls["calls"] += 1
                        loader_calls["wall"] += time.perf_counter() - loader_started
                        loader_calls["cpu"] += time.thread_time() - loader_cpu
                counted_kwargs["loader"] = measured_loader
            started = time.perf_counter()
            cpu = time.thread_time()
            try:
                return original_get(key, **counted_kwargs)
            finally:
                with owner.lock:
                    owner.cache["get." + label] += 1
                    owner.cache["get_wall_seconds"] += time.perf_counter() - started
                    owner.cache["get_thread_cpu_seconds"] += time.thread_time() - cpu
                    owner.cache["loader_calls"] += loader_calls["calls"]
                    owner.cache["loader_wall_seconds"] += loader_calls["wall"]
                    owner.cache["loader_thread_cpu_seconds"] += loader_calls["cpu"]
                    if loader_calls["calls"]:
                        owner.cache["loader_return_size_last"] = loader_calls["size"]
                        owner.cache["loader_return_type_last"] = loader_calls["type"]
                    after = cache_module._CACHE.get(key)
                    present_after = isinstance(after, tuple) and len(after) == 3 and after[0] > now
                    owner.cache["entry_present_after_get"] += int(present_after)
                    owner.cache["entry_absent_after_get"] += int(not present_after)
                    owner.cache["admission_outcome_ambiguous"] += int(label == "loader_or_ambiguous")
                owner._maybe_snapshot()

        def clear_cache(*args: Any, **kwargs: Any) -> Any:
            if not owner.recording:
                return original_clear(*args, **kwargs)
            with owner.lock:
                owner.cache["invalidation_calls"] += 1
            return original_clear(*args, **kwargs)

        cache_module.get_live_probe_cache = get_cache
        cache_module.clear_live_probe_cache = clear_cache
        self.restorers.append(lambda: setattr(cache_module, "get_live_probe_cache", original_get))
        self.restorers.append(lambda: setattr(cache_module, "clear_live_probe_cache", original_clear))
        for loaded in tuple(__import__("sys").modules.values()):
            namespace = getattr(loaded, "__dict__", None)
            if (not isinstance(namespace, dict)
                    or not str(namespace.get("__name__", "")).startswith("fwrouter_api")):
                continue
            for name, value in tuple(namespace.items()):
                if value is original_get:
                    namespace[name] = get_cache
                    self.restorers.append(lambda ns=namespace, n=name, v=original_get: ns.__setitem__(n, v))
                elif value is original_clear:
                    namespace[name] = clear_cache
                    self.restorers.append(lambda ns=namespace, n=name, v=original_clear: ns.__setitem__(n, v))

    def observe_boundary(self, module: Any, name: str, label: str) -> None:
        original = getattr(module, name)
        owner = self

        def observed(*args: Any, **kwargs: Any) -> Any:
            return owner.operation("boundary", label, 0, original, *args, **kwargs)
        setattr(module, name, observed)
        self.restorers.append(lambda: setattr(module, name, original))

    def hash_synthetic_bytes(self, payload: bytes) -> str | None:
        if (not self.recording or not isinstance(payload, bytes)
                or self.hash_bytes + len(payload) > MAX_HASH_BYTES):
            self.hash_truncated = True
            return None
        wall, cpu = time.perf_counter(), time.thread_time()
        digest = hashlib.sha256(payload).hexdigest()
        self.hash_wall += time.perf_counter() - wall
        self.hash_cpu += time.thread_time() - cpu
        self.hash_bytes += len(payload)
        self.hash_samples += 1
        return digest

    def snapshot(self) -> None:
        if not self.enabled:
            return
        with self.snapshot_lock:
            snapshot_wall_start, snapshot_cpu_start = time.perf_counter(), time.thread_time()
            with self.lock:
                payload = {
                    "schema": "fwrouter-startup-causality/v1", "worker_pid": os.getpid(),
                    "active": self.recording, "completed": self.completed,
                    "phase": self.phase, "active_ops": self.active_ops,
                    "groups": copy.deepcopy(self.phases), "cache": dict(self.cache),
                    "overhead": {"bookkeeping_wall_seconds": self.bookkeeping_wall,
                        "operation_observer_wall_seconds": self.operation_observer_wall,
                        "operation_observer_thread_cpu_seconds": self.operation_observer_cpu,
                        "snapshot_wall_seconds_before_latest": self.snapshot_wall,
                        "snapshot_thread_cpu_seconds_before_latest": self.snapshot_cpu},
                    "coverage": {"json": True, "sqlite": True, "live_probe_cache": True,
                    "native_acceptance_rpc": True, "httpx_client_request": True,
                    "mihomo_static_adapter_methods": True, "sql_text_retained": False,
                    "sql_parameters_retained": False, "call_logs": False,
                    "json_size_unit": self.json_size_unit,
                    "cache_admission_is_directly_observed": False},
                    "synthetic_digest_sampling": {"bytes": self.hash_bytes, "samples": self.hash_samples,
                        "wall_seconds": self.hash_wall, "thread_cpu_seconds": self.hash_cpu,
                        "max_bytes": MAX_HASH_BYTES, "truncated": self.hash_truncated,
                        "repeated_calls": self.digest_repeated_calls,
                        "repeated_bytes": self.digest_repeated_bytes,
                        "unique_count": len(self.digest_seen),
                        "by_phase": copy.deepcopy(self.digest_phase_stats),
                        "digest_values_retained": False},
                    "sqlite_statement_repetition": {"by_phase": self.statement_phase_stats,
                        "repeated_calls": self.statement_repeated_calls,
                        "unique_templates": self.statement_unique_templates,
                        "identifiers_or_sql_retained": False},
                    "snapshot_error": self.snapshot_error,
                }
            try:
                self.receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                parent_stat = self.receipt.parent.lstat()
                if (not self.receipt.parent.is_dir() or self.receipt.parent.is_symlink()
                        or parent_stat.st_uid != os.geteuid()):
                    raise OSError("receipt directory ownership invalid")
                temporary = self.receipt.with_suffix(".tmp")
                encoder = self.originals.get("json", {}).get("dumps", json.dumps)
                raw = encoder(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                os.replace(temporary, self.receipt)
                os.chmod(self.receipt, 0o600)
                self.last_snapshot_at = time.monotonic()
            except OSError as exc:
                with self.lock:
                    self.snapshot_error = type(exc).__name__
            finally:
                # The current write's own cost is recorded for the next receipt;
                # the latest write is intentionally not self-reported.
                self.snapshot_wall += time.perf_counter() - snapshot_wall_start
                self.snapshot_cpu += time.thread_time() - snapshot_cpu_start

    def _maybe_snapshot(self) -> None:
        if self.recording and time.monotonic() - self.last_snapshot_at >= 1.0:
            self.snapshot()

    def install(self, *, sqlite_module: Any, json_module: Any, cache_module: Any,
                boundary_targets: tuple[tuple[Any, str, str], ...] = ()) -> None:
        if self.enabled:
            return
        self.enabled = True
        self.observe_json(json_module)
        self.observe_loaded_json_aliases()
        self.observe_sqlite(sqlite_module)
        self.observe_cache(cache_module)
        for module, name, label in boundary_targets:
            self.observe_boundary(module, name, label)
        self.snapshot()

    def restore(self) -> None:
        for restore in reversed(self.restorers):
            try:
                restore()
            except Exception:
                pass
        self.enabled = False
        self.snapshot()
