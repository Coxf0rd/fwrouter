"""Bounded, aggregate-only inspection of explicit offline FWRouter snapshots.

Run with ``python -m fwrouter_api.retention_capacity``. This module deliberately
does not load Settings, initialize the application, or discover live data paths.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any

from fwrouter_api.services.events import known_event_category_for_retention
from fwrouter_api.services.logs_retention import _PROTECTED_SECURITY_EVENT_CODES, _retention_hold


MAX_SNAPSHOT_BYTES = 1_073_741_824
MAX_SCAN_BYTES = 67_108_864
MAX_ROWS = 100_000
MAX_RECORD_BYTES = 65_536
MAX_SCAN_SECONDS = 5.0
_CATEGORIES = ("audit", "operational", "diagnostic", "unknown", "malformed")


class InspectionError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _InvalidArguments(Exception):
    pass


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _InvalidArguments


def _open_snapshot(path_value: str) -> tuple[int, int, os.stat_result, int]:
    """Open a protected regular file by no-follow path walk; return fd and parent fd."""
    candidate = Path(path_value)
    if not candidate.is_absolute() or any(part in {".", ".."} for part in candidate.parts):
        raise InspectionError("INPUT_PATH_INVALID")
    parts = candidate.parts[1:]
    if not parts:
        raise InspectionError("INPUT_PATH_INVALID")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    fd = -1
    try:
        for index, part in enumerate(parts[:-1]):
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  dir_fd=directory_fd)
            except OSError as exc:
                raise InspectionError("INPUT_PATH_UNSAFE") from exc
            info = os.fstat(next_fd)
            sticky_tmp = (info.st_uid == 0 and Path("/" + "/".join(parts[:index + 1]))
                          in {Path("/tmp"), Path("/var/tmp")}
                          and bool(info.st_mode & stat.S_ISVTX))
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                    or (info.st_mode & 0o022 and not sticky_tmp)):
                os.close(next_fd)
                raise InspectionError("INPUT_PATH_UNSAFE")
            os.close(directory_fd)
            directory_fd = next_fd
        parent = os.fstat(directory_fd)
        if (parent.st_uid != os.geteuid() or parent.st_mode & 0o077
                or not stat.S_ISDIR(parent.st_mode)):
            raise InspectionError("INPUT_PARENT_NOT_PRIVATE")
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=directory_fd)
        except OSError as exc:
            raise InspectionError("INPUT_FILE_UNSAFE_OR_MISSING") from exc
        info = os.fstat(fd)
        named = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise InspectionError("INPUT_FILE_NOT_PRIVATE_REGULAR")
        if info.st_size > MAX_SNAPSHOT_BYTES:
            raise InspectionError("SNAPSHOT_BYTE_LIMIT")
        filesystem = os.statvfs(directory_fd)
        result_fd = fd
        fd = -1
        return result_fd, directory_fd, info, filesystem.f_bavail * filesystem.f_frsize
    except Exception:
        if fd >= 0:
            os.close(fd)
        os.close(directory_fd)
        raise


def _sidecar_wal_check(parent_fd: int, filename: str) -> None:
    try:
        info = os.stat(filename + "-wal", dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise InspectionError("SQLITE_WAL_SIDECAR_UNSAFE")
    if info.st_size:
        raise InspectionError("SQLITE_WAL_NOT_CHECKPOINTED")


def _base_source(kind: str, info: os.stat_result, free_bytes: int) -> dict[str, Any]:
    return {
        "source_kind": kind,
        "status": "complete",
        "complete": True,
        "partial_reason": None,
        "snapshot_bytes": info.st_size,
        "filesystem_available_bytes": free_bytes,
    }


def _mark_partial(result: dict[str, Any], reason: str) -> None:
    result.update(status="partial", complete=False, partial_reason=reason)


def _empty_aggregates() -> dict[str, dict[str, Any]]:
    return {
        name: {"records": 0, "logical_bytes": 0, "_timestamps": []}
        for name in _CATEGORIES
    }


def _add_aggregate(
    aggregates: dict[str, dict[str, Any]], name: str, amount: int,
    timestamp: datetime | None,
) -> None:
    aggregate = aggregates[name]
    aggregate["records"] += 1
    aggregate["logical_bytes"] += amount
    if timestamp is not None:
        aggregate["_timestamps"].append(timestamp)


def _finish_aggregates(
    aggregates: dict[str, dict[str, Any]], now: datetime,
) -> dict[str, dict[str, Any]]:
    finished: dict[str, dict[str, Any]] = {}
    for name, aggregate in aggregates.items():
        values = {key: value for key, value in aggregate.items() if key != "_timestamps"}
        values.update(_age_projection(aggregate["_timestamps"], now))
        finished[name] = values
    return finished


def _empty_subgroups() -> dict[str, dict[str, Any]]:
    return {
        name: {"records": 0, "logical_bytes": 0, "_timestamps": []}
        for name in ("security_hold", "terminal_outcome")
    }


def _add_subgroups(
    subgroups: dict[str, dict[str, Any]], code: str | None, amount: int,
    timestamp: datetime | None,
) -> None:
    names = []
    if code in _PROTECTED_SECURITY_EVENT_CODES:
        names.append("security_hold")
    if code == "job.terminal_outcome":
        names.append("terminal_outcome")
    for name in names:
        subgroup = subgroups[name]
        subgroup["records"] += 1
        subgroup["logical_bytes"] += amount
        if timestamp is not None:
            subgroup["_timestamps"].append(timestamp)


def _finish_subgroups(
    subgroups: dict[str, dict[str, Any]], now: datetime,
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            **{key: value for key, value in aggregate.items() if key != "_timestamps"},
            **_age_projection(aggregate["_timestamps"], now),
        }
        for name, aggregate in subgroups.items()
    }


def _event_category(payload: dict[str, Any] | None) -> tuple[str, bool, str | None]:
    if payload is None:
        return "malformed", True, None
    details = payload.get("details")
    held = _retention_hold(payload)
    if details is not None and not isinstance(details, dict):
        code = payload.get("event_code")
        return "malformed", held, code if isinstance(code, str) else None
    if not isinstance(details, dict):
        details = {}
    event_type = str(payload.get("event_type") or "")[:128]
    code_value = payload.get("event_code") or details.get("event_code")
    code = str(code_value or "")[:128]
    classifier_details = dict(details)
    for key in ("event_code", "event_category"):
        if key in payload:
            classifier_details[key] = payload[key]
    category = known_event_category_for_retention(
        event_type, details=classifier_details, event_code=code,
    )
    if category not in {"audit", "operational", "diagnostic"}:
        return "unknown", held, code or None
    return category, held, code or None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _age_projection(timestamps: list[datetime], now: datetime) -> dict[str, Any]:
    if not timestamps:
        return {"oldest_at": None, "newest_at": None, "oldest_age_seconds": None}
    oldest, newest = min(timestamps), max(timestamps)
    return {
        "oldest_at": oldest.isoformat(),
        "newest_at": newest.isoformat(),
        "oldest_age_seconds": max(0, int((now - oldest).total_seconds())),
    }


def inspect_jsonl_snapshot(path_value: str, *, source_kind: str) -> dict[str, Any]:
    if source_kind not in {"operational_jsonl_mirror", "technical_jsonl_diagnostic"}:
        raise InspectionError("JSONL_SOURCE_KIND_INVALID")
    fd, parent_fd, initial, free_bytes = _open_snapshot(path_value)
    now = datetime.now(timezone.utc)
    result = _base_source(source_kind, initial, free_bytes)
    result.update({"records_scanned": 0, "classes": _empty_aggregates(),
                   "subgroups": _empty_subgroups(), "held_records": 0,
                   "oldest_at": None, "newest_at": None, "oldest_age_seconds": None})
    timestamps: list[datetime] = []
    scanned = 0
    started = time.monotonic()
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while True:
                if result["records_scanned"] >= MAX_ROWS:
                    _mark_partial(result, "ROW_LIMIT")
                    break
                if scanned >= MAX_SCAN_BYTES:
                    _mark_partial(result, "BYTE_LIMIT")
                    break
                if time.monotonic() - started >= MAX_SCAN_SECONDS:
                    _mark_partial(result, "TIME_LIMIT")
                    break
                line = stream.readline(min(MAX_RECORD_BYTES + 1, MAX_SCAN_BYTES - scanned))
                if not line:
                    break
                scanned += len(line)
                if len(line) > MAX_RECORD_BYTES or not line.endswith(b"\n") and len(line) == MAX_RECORD_BYTES + 1:
                    _mark_partial(result, "RECORD_BYTE_LIMIT")
                    break
                try:
                    payload = json.loads(line.decode("utf-8"))
                    if not isinstance(payload, dict):
                        payload = None
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    payload = None
                category, held, code = _event_category(payload)
                timestamp = None
                if payload is not None:
                    raw_timestamp = payload.get("timestamp") or payload.get("created_at")
                    timestamp = _parse_time(raw_timestamp)
                    if timestamp is None:
                        category, held = "malformed", True
                _add_aggregate(result["classes"], category, len(line), timestamp)
                if held:
                    result["held_records"] += 1
                _add_subgroups(result["subgroups"], code, len(line), timestamp)
                if timestamp is not None:
                    timestamps.append(timestamp)
                result["records_scanned"] += 1
        final = os.fstat(fd)
        named = os.stat(Path(path_value).name, dir_fd=parent_fd, follow_symlinks=False)
        if ((final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
                != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
                or (named.st_dev, named.st_ino) != (initial.st_dev, initial.st_ino)):
            raise InspectionError("SNAPSHOT_CHANGED_DURING_READ")
        result["scan_bytes"] = scanned
        result["classes"] = _finish_aggregates(result["classes"], now)
        result["subgroups"] = _finish_subgroups(result["subgroups"], now)
        result.update(_age_projection(timestamps, now))
        return result
    except OSError as exc:
        raise InspectionError("SNAPSHOT_READ_FAILED") from exc
    finally:
        os.close(fd)
        os.close(parent_fd)


def _sqlite_table_columns(connection: sqlite3.Connection, name: str) -> set[str] | None:
    object_row = connection.execute(
        "SELECT type FROM sqlite_schema WHERE name = ?", (name,),
    ).fetchone()
    if object_row is None:
        return None
    if object_row[0] != "table":
        raise InspectionError("SQLITE_CANONICAL_SCHEMA_INVALID")
    rows = connection.execute("PRAGMA table_info(" + name + ")").fetchall()
    return {str(row[1]) for row in rows}


def _verify_sqlite_snapshot(
    fd: int, parent_fd: int, filename: str, initial: os.stat_result,
) -> None:
    final = os.fstat(fd)
    named = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    if ((final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
            or (named.st_dev, named.st_ino) != (initial.st_dev, initial.st_ino)):
        raise InspectionError("SNAPSHOT_CHANGED_DURING_READ")
    _sidecar_wal_check(parent_fd, filename)


def inspect_sqlite_snapshot(path_value: str) -> dict[str, Any]:
    fd, parent_fd, initial, free_bytes = _open_snapshot(path_value)
    filename = Path(path_value).name
    started = time.monotonic()
    result = _base_source("sqlite_canonical_snapshot", initial, free_bytes)
    result["database_allocated_bytes"] = 0
    result["database_page_size_bytes"] = None
    result["tables"] = {}
    scanned_bytes = 0
    remaining = MAX_ROWS
    connection: sqlite3.Connection | None = None
    try:
        _sidecar_wal_check(parent_fd, filename)
        uri = "file:/proc/self/fd/" + str(fd) + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=0.1)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")

        def deadline() -> int:
            return int(time.monotonic() - started >= MAX_SCAN_SECONDS)

        connection.set_progress_handler(deadline, 500)
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        result["database_allocated_bytes"] = page_count * page_size
        result["database_page_size_bytes"] = page_size
        table_specs = {
            "jobs": (
                {"job_id", "status", "created_at", "finished_at"},
                "SELECT substr(status,1,32), substr(created_at,1,128), "
                "substr(finished_at,1,128) FROM jobs",
            ),
            "operational_logs": (
                {"event_type", "created_at", "details_json", "message"},
                "SELECT substr(event_type,1,129), length(substr(event_type,1,129)), "
                "typeof(event_type), substr(created_at,1,129), "
                "length(substr(created_at,1,129)), typeof(created_at), "
                "substr(details_json,1,16385), length(substr(details_json,1,16385)), "
                "typeof(details_json), length(CAST(substr(message,1,16385) AS BLOB)), "
                "length(substr(message,1,16385)), typeof(message) FROM operational_logs",
            ),
        }
        schema_tables: dict[str, bool] = {}
        for table, (required, _query) in table_specs.items():
            columns = _sqlite_table_columns(connection, table)
            if columns is None:
                if table == "operational_logs":
                    raise InspectionError("SQLITE_CANONICAL_TABLE_MISSING")
                schema_tables[table] = False
                continue
            if not required <= columns:
                raise InspectionError("SQLITE_CANONICAL_SCHEMA_INVALID")
            schema_tables[table] = True

        now = datetime.now(timezone.utc)
        for table, (_required, query) in table_specs.items():
            if not schema_tables[table]:
                continue
            counts: dict[str, int] = {}
            rows_seen = 0
            timestamps: list[datetime] = []
            logical_bytes = 0
            classes = _empty_aggregates() if table == "operational_logs" else None
            subgroups = _empty_subgroups() if table == "operational_logs" else None
            held_rows = 0
            for row in connection.execute(query):
                if remaining <= 0:
                    _mark_partial(result, "ROW_LIMIT")
                    break
                if time.monotonic() - started >= MAX_SCAN_SECONDS:
                    _mark_partial(result, "TIME_LIMIT")
                    break
                timestamp: datetime | None = None
                code: str | None = None
                category = "malformed"
                held = True
                if table == "jobs":
                    status_value, created_at_value, finished_at_value = row
                    values = (status_value, created_at_value, finished_at_value)
                    row_bytes = sum(len(value.encode("utf-8")) for value in values if isinstance(value, str))
                    if any(value is not None and not isinstance(value, str) for value in values):
                        _mark_partial(result, "RECORD_BYTE_LIMIT")
                        break
                    if row_bytes > MAX_RECORD_BYTES:
                        _mark_partial(result, "RECORD_BYTE_LIMIT")
                        break
                    if scanned_bytes + row_bytes > MAX_SCAN_BYTES:
                        _mark_partial(result, "BYTE_LIMIT")
                        break
                    status = status_value if status_value in {
                        "queued", "running", "success", "failed", "cancelled",
                    } else "other"
                    counts[status] = counts.get(status, 0) + 1
                    for value in (created_at_value, finished_at_value):
                        timestamp = _parse_time(value)
                        if timestamp is not None:
                            timestamps.append(timestamp)
                else:
                    (event_type, event_type_chars, event_type_type,
                     created_at, created_at_chars, created_at_type,
                     details_json, details_chars, details_type,
                     message_bytes, message_chars, message_type) = row
                    content_values = (event_type, created_at, details_json)
                    content_lengths = [
                        len(value.encode("utf-8")) if isinstance(value, str)
                        else len(value) if isinstance(value, bytes)
                        else 0
                        for value in content_values
                    ]
                    row_bytes = sum(content_lengths) + int(message_bytes or 0)
                    truncated = (
                        int(event_type_chars or 0) > 128
                        or int(created_at_chars or 0) > 128
                        or int(details_chars or 0) > 16_384
                        or int(message_chars or 0) > 16_384
                    )
                    if truncated or row_bytes > MAX_RECORD_BYTES:
                        _mark_partial(result, "RECORD_BYTE_LIMIT")
                        break
                    if scanned_bytes + row_bytes > MAX_SCAN_BYTES:
                        _mark_partial(result, "BYTE_LIMIT")
                        break
                    type_mismatch = (
                        event_type_type != "text" or created_at_type != "text"
                        or details_type not in {"text", "null"} or message_type != "text"
                        or not isinstance(event_type, str) or not isinstance(created_at, str)
                        or (details_type == "text" and not isinstance(details_json, str))
                        or (details_type == "null" and details_json is not None)
                    )
                    try:
                        if type_mismatch:
                            details = details_json
                        elif details_type == "null":
                            details = None
                        else:
                            details = json.loads(details_json) if details_json else None
                    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
                        details = details_json
                    payload = {"event_type": event_type, "created_at": created_at, "details": details}
                    if type_mismatch:
                        code_value = details.get("event_code") if isinstance(details, dict) else None
                        code = code_value if isinstance(code_value, str) else None
                        category, held = "malformed", _retention_hold(payload)
                    else:
                        category, held, code = _event_category(payload)
                    timestamp = _parse_time(created_at)
                    logical_bytes += row_bytes
                    if timestamp is None:
                        category, held = "malformed", True
                    counts[category] = counts.get(category, 0) + 1
                    if classes is not None:
                        _add_aggregate(classes, category, row_bytes, timestamp)
                    if held:
                        held_rows += 1
                    if subgroups is not None:
                        _add_subgroups(subgroups, code, row_bytes, timestamp)
                    if timestamp is not None:
                        timestamps.append(timestamp)
                if table == "jobs":
                    logical_bytes += row_bytes
                scanned_bytes += row_bytes
                remaining -= 1
                rows_seen += 1
            table_result: dict[str, Any] = {
                "rows_scanned": rows_seen,
                "logical_bytes_scanned": logical_bytes,
                "counts": counts if table == "jobs" else {
                    **{name: value["records"] for name, value in (classes or {}).items()},
                    "held": held_rows,
                },
                **_age_projection(timestamps, now),
            }
            if classes is not None and subgroups is not None:
                table_result["classes"] = _finish_aggregates(classes, now)
                table_result["subgroups"] = _finish_subgroups(subgroups, now)
            result["tables"][table] = table_result
            if not result["complete"]:
                break
        result["rows_scanned"] = sum(item["rows_scanned"] for item in result["tables"].values())
        result["scan_bytes"] = scanned_bytes
        result["scan_seconds_limit"] = MAX_SCAN_SECONDS
        _verify_sqlite_snapshot(fd, parent_fd, filename, initial)
        return result
    except InspectionError:
        raise
    except sqlite3.Error as exc:
        if "interrupted" in str(exc).lower():
            _mark_partial(result, "TIME_LIMIT")
            result["rows_scanned"] = sum(item["rows_scanned"] for item in result["tables"].values())
            result["scan_bytes"] = scanned_bytes
            _verify_sqlite_snapshot(fd, parent_fd, filename, initial)
            return result
        raise InspectionError("SQLITE_SNAPSHOT_INVALID_OR_UNREADABLE") from exc
    finally:
        if connection is not None:
            connection.close()
        os.close(fd)
        os.close(parent_fd)


def _forecast(args: argparse.Namespace) -> dict[str, Any] | None:
    supplied = (args.forecast_rate_bytes_per_day, args.forecast_budget_bytes,
                args.forecast_reserve_bytes, args.forecast_horizon_days)
    if all(value is None for value in supplied):
        return None
    if any(value is None for value in supplied):
        raise InspectionError("FORECAST_ASSUMPTIONS_INCOMPLETE")
    rate, budget, reserve, horizon = supplied
    if (any(not math.isfinite(value) for value in supplied)
            or any(value < 0 for value in supplied) or budget < reserve or horizon <= 0):
        raise InspectionError("FORECAST_ASSUMPTIONS_INVALID")
    projected = int(rate * horizon)
    growth_allowance = budget - reserve
    forecast = {
        "basis": "caller_assumed_incremental_growth_budget",
        "assumed_net_growth_bytes_per_day": rate,
        "incremental_growth_budget_bytes": budget,
        "reserve_bytes": reserve,
        "horizon_days": horizon,
        "projected_growth_bytes": projected,
        "remaining_incremental_growth_allowance_bytes": max(0, growth_allowance - projected),
        "exceeds_incremental_growth_allowance": projected > growth_allowance,
    }
    return forecast


def main(argv: list[str] | None = None) -> int:
    parser = _SanitizedArgumentParser(
        description="Inspect explicit protected offline snapshots using bounded aggregate-only reads."
    )
    parser.add_argument("--sqlite-snapshot", help="Protected checkpointed SQLite snapshot (no live DB).")
    parser.add_argument("--operational-jsonl-snapshot", help="Optional protected operational JSONL mirror snapshot.")
    parser.add_argument("--technical-jsonl-snapshot", help="Optional protected technical JSONL diagnostic snapshot.")
    parser.add_argument("--forecast-rate-bytes-per-day", type=float)
    parser.add_argument("--forecast-budget-bytes", type=int)
    parser.add_argument("--forecast-reserve-bytes", type=int)
    parser.add_argument("--forecast-horizon-days", type=float)
    try:
        args = parser.parse_args(argv)
    except _InvalidArguments:
        print(json.dumps({"ok": False, "status": "error", "error_code": "ARGUMENTS_INVALID"},
                         sort_keys=True, separators=(",", ":")))
        return 2
    if not any((args.sqlite_snapshot, args.operational_jsonl_snapshot, args.technical_jsonl_snapshot)):
        print(json.dumps({"ok": False, "status": "error", "error_code": "SNAPSHOT_INPUT_REQUIRED"},
                         sort_keys=True, separators=(",", ":")))
        return 2
    try:
        sources: list[dict[str, Any]] = []
        if args.sqlite_snapshot:
            sources.append(inspect_sqlite_snapshot(args.sqlite_snapshot))
        if args.operational_jsonl_snapshot:
            sources.append(inspect_jsonl_snapshot(args.operational_jsonl_snapshot,
                                                  source_kind="operational_jsonl_mirror"))
        if args.technical_jsonl_snapshot:
            sources.append(inspect_jsonl_snapshot(args.technical_jsonl_snapshot,
                                                  source_kind="technical_jsonl_diagnostic"))
        forecast = _forecast(args)
        complete = all(item["complete"] for item in sources)
        output = {"ok": complete, "status": "complete" if complete else "partial",
                  "sources_are_reported_separately": True, "sources": sources, "forecast": forecast}
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0 if complete else 2
    except InspectionError as exc:
        print(json.dumps({"ok": False, "status": "error", "error_code": exc.code}, sort_keys=True,
                         separators=(",", ":")))
        return 1
    except (OSError, ValueError, OverflowError):
        print(json.dumps({"ok": False, "status": "error", "error_code": "INSPECTION_FAILED"}, sort_keys=True,
                         separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
