from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from _test_support import configure_test_state_dir
from fwrouter_api import retention_capacity as capacity
from fwrouter_api.db.connection import db_session, get_db_path, initialize_database
from fwrouter_api.services.database_backup import create_sqlite_backup
from fwrouter_api.services.jobs import create_job, mark_job_running, mark_job_success
from fwrouter_api.services.jobs_retention import cleanup_jobs_retention


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _private_file(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _sqlite_snapshot(path: Path) -> Path:
    _private_dir(path.parent)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE jobs(job_id TEXT, status TEXT, created_at TEXT, finished_at TEXT);"
            "CREATE TABLE operational_logs(event_type TEXT, created_at TEXT, details_json TEXT, message TEXT);"
            "INSERT INTO jobs VALUES ('secret-job-id','success','2026-10-27T00:00:00Z','2026-10-27T00:01:00Z');"
            "INSERT INTO operational_logs VALUES ('job.terminal_outcome','2026-10-27T00:01:00Z',"
            "'{\"event_code\":\"job.terminal_outcome\"}','secret-message');"
        )
    path.chmod(0o600)
    return path


def test_terminal_outcome_snapshot_is_classified_as_held_audit_and_never_echoed(tmp_path: Path) -> None:
    source = _private_file(
        _private_dir(tmp_path / "snapshots") / "operational.jsonl",
        (json.dumps({
            "event_id": "secret-event-id", "event_type": "job.terminal_outcome",
            "event_code": "job.terminal_outcome", "created_at": "2026-10-27T00:01:00Z",
            "message": "secret-message", "details": {"event_code": "job.terminal_outcome",
            "action": "job.terminal_outcome", "event_category": "audit"},
        }) + "\n" + "{broken secret-malformed-payload}\n").encode(),
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    mode = source.stat().st_mode & 0o777
    children = sorted(item.name for item in source.parent.iterdir())

    result = capacity.inspect_jsonl_snapshot(str(source), source_kind="operational_jsonl_mirror")
    encoded = json.dumps(result, sort_keys=True)

    assert result["complete"] is True
    assert result["classes"]["audit"]["records"] == 1
    assert result["classes"]["malformed"]["records"] == 1
    assert result["held_records"] == 2  # audit and malformed rows are both retained by policy
    assert result["oldest_at"] == "2026-10-27T00:01:00+00:00"
    for secret in ("secret-event-id", "secret-job-id", "secret-message", "secret-malformed-payload"):
        assert secret not in encoded
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert source.stat().st_mode & 0o777 == mode
    assert sorted(item.name for item in source.parent.iterdir()) == children


def test_top_level_metadata_conflict_and_nonobject_details_follow_retention_hold(tmp_path: Path) -> None:
    records = [
        {"event_type": "job.terminal_outcome", "event_code": "job.terminal_outcome",
         "event_category": "diagnostic", "details": {"event_code": "job.terminal_outcome",
         "event_category": "audit"}},
        {"event_type": "job.terminal_outcome", "event_code": "job.terminal_outcome",
         "details": ["secret-nested-value"]},
    ]
    source = _private_file(
        _private_dir(tmp_path / "snapshots") / "operational.jsonl",
        b"".join((json.dumps(record) + "\n").encode() for record in records),
    )
    result = capacity.inspect_jsonl_snapshot(str(source), source_kind="operational_jsonl_mirror")
    assert result["held_records"] == 2
    assert result["classes"]["malformed"]["records"] == 2
    assert result["classes"]["audit"]["records"] == 0
    assert result["subgroups"]["terminal_outcome"]["records"] == 2
    assert "secret-nested-value" not in json.dumps(result)


def test_sqlite_snapshot_is_immutable_and_reports_only_fixed_aggregates(tmp_path: Path) -> None:
    source = _sqlite_snapshot(tmp_path / "db" / "snapshot.db")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    children = sorted(item.name for item in source.parent.iterdir())

    result = capacity.inspect_sqlite_snapshot(str(source))
    encoded = json.dumps(result, sort_keys=True)

    assert result["complete"] is True
    assert result["source_kind"] == "sqlite_canonical_snapshot"
    assert result["tables"]["jobs"]["counts"] == {"success": 1}
    assert result["tables"]["operational_logs"]["counts"] == {
        "audit": 1, "operational": 0, "diagnostic": 0, "unknown": 0, "malformed": 0, "held": 1,
    }
    for secret in ("secret-job-id", "secret-message", "job.terminal_outcome"):
        assert secret not in encoded
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert sorted(item.name for item in source.parent.iterdir()) == children
    assert not (source.parent / "snapshot.db-wal").exists()
    assert not (source.parent / "snapshot.db-shm").exists()


def test_snapshot_reads_are_concurrent_and_do_not_create_coordination_files(tmp_path: Path) -> None:
    source = _sqlite_snapshot(tmp_path / "db" / "snapshot.db")
    children = sorted(item.name for item in source.parent.iterdir())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: capacity.inspect_sqlite_snapshot(str(source)), range(2)))
    assert all(result["complete"] for result in results)
    assert results[0]["tables"] == results[1]["tables"]
    assert sorted(item.name for item in source.parent.iterdir()) == children


def test_symlink_and_nonempty_wal_are_rejected_with_sanitized_codes(tmp_path: Path) -> None:
    target = _sqlite_snapshot(tmp_path / "db" / "snapshot.db")
    link = target.parent / "alias.db"
    link.symlink_to(target.resolve())
    with pytest.raises(capacity.InspectionError, match="INPUT_FILE_UNSAFE_OR_MISSING"):
        capacity.inspect_sqlite_snapshot(str(link))

    wal = target.parent / "snapshot.db-wal"
    _private_file(wal, b"uncheckpointed test bytes")
    with pytest.raises(capacity.InspectionError, match="SQLITE_WAL_NOT_CHECKPOINTED"):
        capacity.inspect_sqlite_snapshot(str(target))
    assert wal.read_bytes() == b"uncheckpointed test bytes"


def test_fifo_open_is_nonblocking_before_file_type_check(tmp_path: Path, monkeypatch) -> None:
    parent = _private_dir(tmp_path / "snapshots")
    fifo = parent / "input.jsonl"
    os.mkfifo(fifo, 0o600)
    real_open = os.open
    checked = False

    def guarded_open(path, flags, *args, **kwargs):
        nonlocal checked
        if path == fifo.name and kwargs.get("dir_fd") is not None:
            assert flags & os.O_NONBLOCK
            checked = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(capacity.os, "open", guarded_open)
    with pytest.raises(capacity.InspectionError, match="INPUT_FILE_NOT_PRIVATE_REGULAR"):
        capacity.inspect_jsonl_snapshot(str(fifo), source_kind="technical_jsonl_diagnostic")
    assert checked


def test_sqlite_requires_canonical_table_but_jobs_are_optional(tmp_path: Path) -> None:
    snapshots = _private_dir(tmp_path / "db")
    missing = snapshots / "missing.db"
    with sqlite3.connect(missing) as connection:
        connection.execute("CREATE TABLE jobs(job_id, status, created_at, finished_at)")
    missing.chmod(0o600)
    with pytest.raises(capacity.InspectionError, match="SQLITE_CANONICAL_TABLE_MISSING"):
        capacity.inspect_sqlite_snapshot(str(missing))

    view = snapshots / "view.db"
    with sqlite3.connect(view) as connection:
        connection.execute("CREATE TABLE base(event_type, created_at, details_json, message)")
        connection.execute("CREATE VIEW operational_logs AS SELECT * FROM base")
    view.chmod(0o600)
    with pytest.raises(capacity.InspectionError, match="SQLITE_CANONICAL_SCHEMA_INVALID"):
        capacity.inspect_sqlite_snapshot(str(view))

    valid = snapshots / "valid.db"
    with sqlite3.connect(valid) as connection:
        connection.execute(
            "CREATE TABLE operational_logs(event_type TEXT, created_at TEXT, details_json TEXT, message TEXT)"
        )
    valid.chmod(0o600)
    result = capacity.inspect_sqlite_snapshot(str(valid))
    assert result["complete"] is True
    assert "jobs" not in result["tables"]
    assert result["tables"]["operational_logs"]["rows_scanned"] == 0


def test_sqlite_malformed_blob_classification_has_fixed_age_and_subgroups(tmp_path: Path) -> None:
    source = _sqlite_snapshot(tmp_path / "db" / "snapshot.db")
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO operational_logs VALUES (?, ?, ?, ?)",
            ("job.terminal_outcome", "2026-10-26T00:00:00Z", sqlite3.Binary(b"secret-blob"), "private"),
        )
        connection.execute(
            "INSERT INTO operational_logs VALUES (?, ?, ?, ?)",
            ("job.terminal_outcome", "2026-10-25T00:00:00Z", '["secret-array"]', "private"),
        )
    result = capacity.inspect_sqlite_snapshot(str(source))
    table = result["tables"]["operational_logs"]
    encoded = json.dumps(result)
    assert table["classes"]["malformed"]["records"] == 2
    assert table["classes"]["audit"]["records"] == 1
    assert table["subgroups"]["terminal_outcome"]["records"] == 1
    assert table["counts"]["held"] == 3
    assert table["classes"]["malformed"]["oldest_age_seconds"] is not None
    assert "secret-blob" not in encoded and "secret-array" not in encoded


def test_sqlite_byte_bound_does_not_account_the_row_that_exceeds_it(tmp_path: Path, monkeypatch) -> None:
    source = _sqlite_snapshot(tmp_path / "db" / "snapshot.db")
    monkeypatch.setattr(capacity, "MAX_SCAN_BYTES", 2)
    result = capacity.inspect_sqlite_snapshot(str(source))
    assert result["complete"] is False
    assert result["partial_reason"] == "BYTE_LIMIT"
    table = result["tables"]["jobs"]
    assert table["rows_scanned"] == 0
    assert table["logical_bytes_scanned"] == 0
    assert result["scan_bytes"] == 0


def test_schema27_terminal_audit_survives_job_retention_and_snapshot_backup(
    monkeypatch, tmp_path: Path,
) -> None:
    configure_test_state_dir(monkeypatch, tmp_path)
    initialize_database()
    job = create_job("rules_full_update", requested_by="pytest", input_data={"requested_by": "pytest"})
    mark_job_running(job["job_id"])
    mark_job_success(job["job_id"], result={"private": "secret-result"})
    with db_session() as connection:
        schema = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        assert schema == "27"
        connection.execute(
            "UPDATE jobs SET created_at=datetime('now','-10 days'), "
            "finished_at=datetime('now','-10 days'), updated_at=datetime('now','-10 days') "
            "WHERE job_id=?", (job["job_id"],),
        )
        snapshot = connection.execute(
            "SELECT event_id,details_json FROM operational_logs WHERE event_type='job.terminal_outcome'"
        ).fetchone()
        details = json.loads(snapshot["details_json"])
        details["finished_at_utc"] = connection.execute(
            "SELECT finished_at FROM jobs WHERE job_id=?", (job["job_id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE operational_logs SET details_json=? WHERE event_id=?",
            (json.dumps(details), snapshot["event_id"]),
        )
    result = cleanup_jobs_retention(dry_run=False)
    assert result["deleted_jobs_count"] == 1
    with db_session() as connection:
        assert connection.execute("SELECT 1 FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone() is None
    backup_dir = _private_dir(tmp_path / "protected-backups")
    backup = create_sqlite_backup(get_db_path(), backup_dir, prefix="capacity-test-")
    inspected = capacity.inspect_sqlite_snapshot(backup["backup_path"])
    table = inspected["tables"]["operational_logs"]
    encoded = json.dumps(inspected)
    assert inspected["complete"] is True
    assert table["subgroups"]["terminal_outcome"]["records"] == 1
    assert table["counts"]["held"] >= 1
    assert "secret-result" not in encoded and job["job_id"] not in encoded


def test_jsonl_row_and_record_bounds_are_explicit_and_incomplete(tmp_path: Path, monkeypatch) -> None:
    source = _private_file(
        _private_dir(tmp_path / "snapshots") / "technical.jsonl",
        b'{"event_type":"probe_result","timestamp":"2026-10-27T00:00:00Z"}\n'
        b'{"event_type":"probe_result","timestamp":"2026-10-27T00:00:01Z"}\n',
    )
    monkeypatch.setattr(capacity, "MAX_ROWS", 1)
    result = capacity.inspect_jsonl_snapshot(str(source), source_kind="technical_jsonl_diagnostic")
    assert result["complete"] is False
    assert result["partial_reason"] == "ROW_LIMIT"
    assert result["records_scanned"] == 1

    oversized = _private_file(source.parent / "oversized.jsonl", b"x" * (capacity.MAX_RECORD_BYTES + 2))
    limited = capacity.inspect_jsonl_snapshot(str(oversized), source_kind="technical_jsonl_diagnostic")
    assert limited["complete"] is False
    assert limited["partial_reason"] == "RECORD_BYTE_LIMIT"


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"])
def test_out_of_range_timezone_timestamps_are_malformed_and_held(tmp_path: Path, value: str) -> None:
    assert capacity._parse_time(value) is None
    jsonl = _private_file(
        _private_dir(tmp_path / "jsonl") / "events.jsonl",
        (json.dumps({"event_type": "job.terminal_outcome", "event_code": "job.terminal_outcome",
                    "created_at": value, "details": {"event_code": "job.terminal_outcome",
                    "event_category": "audit"}}) + "\n").encode(),
    )
    jsonl_result = capacity.inspect_jsonl_snapshot(str(jsonl), source_kind="operational_jsonl_mirror")
    assert jsonl_result["held_records"] == 1
    assert jsonl_result["classes"]["malformed"]["records"] == 1

    sqlite_path = _private_dir(tmp_path / "sqlite") / "snapshot.db"
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "CREATE TABLE operational_logs(event_type TEXT, created_at TEXT, details_json TEXT, message TEXT)"
        )
        connection.execute(
            "INSERT INTO operational_logs VALUES (?, ?, ?, ?)",
            ("job.terminal_outcome", value, json.dumps({"event_code": "job.terminal_outcome",
             "event_category": "audit"}), "secret-message"),
        )
    sqlite_path.chmod(0o600)
    sqlite_result = capacity.inspect_sqlite_snapshot(str(sqlite_path))
    table = sqlite_result["tables"]["operational_logs"]
    assert table["counts"]["malformed"] == 1 and table["counts"]["held"] == 1


def test_missing_or_empty_event_timestamps_are_malformed_and_held_in_both_formats(tmp_path: Path) -> None:
    invalid_values = [{"missing": True}, {"timestamp": None}, {"timestamp": ""},
                      {"timestamp": False}, {"timestamp": 0}, {"timestamp": 123}]
    jsonl_records = []
    for value in invalid_values:
        record = {"event_type": "apply_failed", "event_code": "apply_failed",
                  "details": {"event_code": "apply_failed", "event_category": "operational"}}
        if not value.get("missing"):
            record["created_at"] = value["timestamp"]
        jsonl_records.append(record)
    jsonl_records.append({"event_type": "apply_failed", "event_code": "apply_failed",
                         "created_at": "2026-10-27T00:00:00Z",
                         "details": {"event_code": "apply_failed", "event_category": "operational"}})
    jsonl = _private_file(
        _private_dir(tmp_path / "jsonl") / "events.jsonl",
        b"".join((json.dumps(record) + "\n").encode() for record in jsonl_records),
    )
    jsonl_result = capacity.inspect_jsonl_snapshot(str(jsonl), source_kind="operational_jsonl_mirror")
    assert jsonl_result["classes"]["malformed"]["records"] == 6
    assert jsonl_result["classes"]["operational"]["records"] == 1
    assert jsonl_result["held_records"] == 6

    sqlite_path = _private_dir(tmp_path / "sqlite") / "snapshot.db"
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "CREATE TABLE operational_logs(event_type TEXT, created_at, details_json TEXT, message TEXT)"
        )
        connection.executemany(
            "INSERT INTO operational_logs VALUES (?, ?, ?, ?)",
            [("apply_failed", None if value.get("missing") else value.get("timestamp"),
              json.dumps({"event_code": "apply_failed", "event_category": "operational"}), "secret")
             for value in invalid_values]
            + [("apply_failed", "2026-10-27T00:00:00Z",
                json.dumps({"event_code": "apply_failed", "event_category": "operational"}), "secret")],
        )
    sqlite_path.chmod(0o600)
    sqlite_result = capacity.inspect_sqlite_snapshot(str(sqlite_path))
    table = sqlite_result["tables"]["operational_logs"]
    assert table["counts"]["malformed"] == 6
    assert table["counts"]["operational"] == 1
    assert table["counts"]["held"] == 6


def test_sqlite_early_metadata_interruption_returns_bounded_partial(tmp_path: Path, monkeypatch) -> None:
    source = _private_dir(tmp_path / "db") / "metadata.db"
    columns = ",".join(f"column_{index} TEXT" for index in range(1500))
    with sqlite3.connect(source) as connection:
        connection.execute(f"CREATE TABLE operational_logs({columns})")
    source.chmod(0o600)
    monkeypatch.setattr(capacity, "MAX_SCAN_SECONDS", 0)
    result = capacity.inspect_sqlite_snapshot(str(source))
    assert result["complete"] is False
    assert result["status"] == "partial" and result["partial_reason"] == "TIME_LIMIT"
    assert result["rows_scanned"] == 0 and result["scan_bytes"] == 0
    assert json.dumps(result) and "metadata.db" not in json.dumps(result)


def test_sqlite_row_and_record_bounds_never_claim_complete_totals(tmp_path: Path, monkeypatch) -> None:
    source = _sqlite_snapshot(tmp_path / "db" / "snapshot.db")
    monkeypatch.setattr(capacity, "MAX_ROWS", 1)
    limited = capacity.inspect_sqlite_snapshot(str(source))
    assert limited["complete"] is False
    assert limited["partial_reason"] == "ROW_LIMIT"

    large = _private_dir(tmp_path / "large") / "snapshot.db"
    with sqlite3.connect(large) as connection:
        connection.executescript(
            "CREATE TABLE operational_logs(event_type TEXT, created_at TEXT, details_json TEXT, message TEXT);"
        )
        connection.execute(
            "INSERT INTO operational_logs VALUES (?, ?, ?, ?)",
            ("runtime_failed", "2026-10-27T00:00:00Z", json.dumps({"note": "x" * 64_000}), "m" * 2_000),
        )
    large.chmod(0o600)
    monkeypatch.setattr(capacity, "MAX_ROWS", 100)
    oversized = capacity.inspect_sqlite_snapshot(str(large))
    assert oversized["complete"] is False
    assert oversized["partial_reason"] == "RECORD_BYTE_LIMIT"


def test_jsonl_read_failure_is_sanitized_and_leaves_source_unchanged(tmp_path: Path, monkeypatch) -> None:
    source = _private_file(
        _private_dir(tmp_path / "snapshots") / "technical.jsonl",
        b'{"event_type":"probe_result"}\n',
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    real_fdopen = os.fdopen

    class FailingReader:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self, _size):
            raise OSError("secret path and contents")

    monkeypatch.setattr(capacity.os, "fdopen", lambda *_args, **_kwargs: FailingReader())
    with pytest.raises(capacity.InspectionError, match="SNAPSHOT_READ_FAILED") as raised:
        capacity.inspect_jsonl_snapshot(str(source), source_kind="technical_jsonl_diagnostic")
    assert "secret path and contents" not in str(raised.value)
    monkeypatch.setattr(capacity.os, "fdopen", real_fdopen)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert sorted(item.name for item in source.parent.iterdir()) == ["technical.jsonl"]


def test_jsonl_snapshot_replacement_during_read_is_not_reported_complete(
    tmp_path: Path, monkeypatch,
) -> None:
    source = _private_file(
        _private_dir(tmp_path / "snapshots") / "technical.jsonl",
        b'{"event_type":"probe_result"}\n',
    )
    real_fdopen = os.fdopen

    class ReplacingReader:
        def __init__(self, fd: int, mode: str, *, closefd: bool) -> None:
            self.handle = real_fdopen(fd, mode, closefd=closefd)
            self.replaced = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def readline(self, size: int) -> bytes:
            value = self.handle.readline(size)
            if not self.replaced:
                with source.open("ab") as output:
                    output.write(b'{"event_type":"reconcile_drift"}\n')
                self.replaced = True
            return value

    monkeypatch.setattr(
        capacity.os, "fdopen", lambda fd, mode, closefd=True: ReplacingReader(fd, mode, closefd=closefd),
    )
    with pytest.raises(capacity.InspectionError, match="SNAPSHOT_CHANGED_DURING_READ"):
        capacity.inspect_jsonl_snapshot(str(source), source_kind="technical_jsonl_diagnostic")


def test_forecast_requires_explicit_complete_finite_assumptions() -> None:
    args = type("Args", (), {
        "forecast_rate_bytes_per_day": 100.0,
        "forecast_budget_bytes": 10_000,
        "forecast_reserve_bytes": 1_000,
        "forecast_horizon_days": 5.0,
    })()
    result = capacity._forecast(args)
    assert result["basis"] == "caller_assumed_incremental_growth_budget"
    assert result["projected_growth_bytes"] == 500
    assert result["remaining_incremental_growth_allowance_bytes"] == 8_500

    args.forecast_rate_bytes_per_day = 0
    assert capacity._forecast(args)["projected_growth_bytes"] == 0

    args.forecast_rate_bytes_per_day = float("nan")
    with pytest.raises(capacity.InspectionError, match="FORECAST_ASSUMPTIONS_INVALID"):
        capacity._forecast(args)


def test_standalone_entrypoint_requires_explicit_snapshot_and_labels_projection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = _private_file(
        _private_dir(tmp_path / "snapshots") / "technical.jsonl",
        b'{"event_type":"probe_result","timestamp":"2026-10-27T00:00:00Z"}\n',
    )
    code = capacity.main([
        "--technical-jsonl-snapshot", str(source),
        "--forecast-rate-bytes-per-day", "100",
        "--forecast-budget-bytes", "10000",
        "--forecast-reserve-bytes", "1000",
        "--forecast-horizon-days", "5",
    ])
    output = capsys.readouterr().out
    report = json.loads(output)
    assert code == 0
    assert report["sources"][0]["source_kind"] == "technical_jsonl_diagnostic"
    assert report["forecast"]["basis"] == "caller_assumed_incremental_growth_budget"
    assert str(source) not in output


def test_partial_cli_output_and_invalid_arguments_are_sanitized(tmp_path: Path, capsys, monkeypatch) -> None:
    source = _private_file(
        _private_dir(tmp_path / "snapshots") / "technical.jsonl",
        b'{"event_type":"probe_result"}\n{"event_type":"probe_result"}\n',
    )
    monkeypatch.setattr(capacity, "MAX_ROWS", 1)
    code = capacity.main(["--technical-jsonl-snapshot", str(source)])
    output = capsys.readouterr().out
    assert code == 2
    report = json.loads(output)
    assert report["status"] == "partial" and report["ok"] is False
    assert report["sources"][0]["complete"] is False

    code = capacity.main(["--technical-jsonl-snapshot", "/private/caller/path", "--bogus"])
    output = capsys.readouterr().out
    assert code == 2
    assert json.loads(output) == {"error_code": "ARGUMENTS_INVALID", "ok": False, "status": "error"}
    assert "/private/caller/path" not in output and "bogus" not in output
