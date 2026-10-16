from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.events import write_operational_event
from fwrouter_api.core.config import get_settings
from fwrouter_api.services.event_contract import (
    MAX_EVENT_DETAILS_BYTES,
    normalize_event_details,
    reset_event_context,
    sanitize_value,
    set_event_context,
    scrub_jsonl_files,
)
from fwrouter_api.services import event_contract
from fwrouter_api.services.logs import (
    list_technical_logs,
    write_operational_log,
    write_operational_log_in_connection,
    write_technical_log,
)


def test_recursive_sanitizer_redacts_credentials_but_preserves_safe_ids() -> None:
    safe = sanitize_value({
        "server_id": "server-1",
        "subscription_uri": "vless://secret@example.test?token=abc",
        "nested": {"authorization": "Bearer abc", "endpoint": "https://u:p@example.test/x?password=pw"},
    })
    encoded = json.dumps(safe)
    assert safe["server_id"] == "server-1"
    assert "secret@" not in encoded and "token=abc" not in encoded
    assert "Bearer abc" not in encoded and "u:p@" not in encoded and "password=pw" not in encoded
    message = sanitize_value({"message": "endpoint=https://user:pass@example.test/path?token=secret"})["message"]
    assert message.startswith("endpoint=https://")
    assert "user:pass" not in message and "token=secret" not in message


def test_large_event_details_keep_error_and_correlation_envelope() -> None:
    details = normalize_event_details(
        {
            "error_code": "PROBE_FAILED",
            "error_reason": "timeout",
            "error_message": "controller timeout",
            "stage": "verification",
            "request_id": "req-1",
            "payload": "x" * (300 * 1024),
        },
        event_id="event-1",
        timestamp="2026-09-26T00:00:00+00:00",
        severity="warning",
        component="test",
        event_category="diagnostic",
        event_code="PROBE_FAILED",
        event_type="probe_failed",
    )
    assert details["truncated_payload"] is True
    assert len(details["payload_sha256"]) == 64
    assert details["original_bytes"] > 256 * 1024
    assert details["error_code"] == "PROBE_FAILED"
    assert details["error_reason"] == "timeout"
    assert details["error_message"] == "controller timeout"
    assert details["stage"] == "verification"
    assert details["request_id"] == "req-1"


def test_extreme_diagnostics_and_key_counts_stay_within_envelope_limit() -> None:
    details = normalize_event_details(
        {
            "error_code": "RUNTIME_FAILED",
            "error_reason": "r" * (400 * 1024),
            "error_message": "m" * (400 * 1024),
            "phase": "verification",
            "workflow_id": "workflow-safe",
            **{f"field_{index:04d}": index for index in range(2500)},
        },
        event_id="event-extreme",
        timestamp="2026-09-26T00:00:00+00:00",
        severity="error",
        component="test",
        event_category="diagnostic",
        event_code="RUNTIME_FAILED",
        event_type="runtime_failed",
    )
    assert len(json.dumps(details, ensure_ascii=False).encode("utf-8")) <= MAX_EVENT_DETAILS_BYTES
    assert details["error_code"] == "RUNTIME_FAILED"
    assert details["phase"] == "verification"
    assert details["workflow_id"] == "workflow-safe"
    assert details["payload_key_count"] == 2513
    assert details["payload_keys_truncated"] is True


def test_technical_writer_persists_event_id_and_sanitizes_message() -> None:
    context_token = set_event_context(
        request_id="request-top-level", job_id="job-top-level",
        workflow_id="workflow-top-level", causation_id="cause-top-level",
    )
    try:
        written = write_technical_log(
            component="event-contract-test",
            event_type="safe_message_probe",
            level="warning",
            message="failed https://user:password@example.test/sub?token=secret",
            details={"password": "sensitive", "server_id": "server-safe"},
        )
        operational = write_operational_log(
            event_type="safe_operational_probe", message="Operational envelope."
        )
    finally:
        reset_event_context(context_token)
    listed = next(
        item for item in list_technical_logs(limit=100)
        if item["event_type"] == "safe_message_probe"
    )
    assert written["event_id"] == listed["event_id"]
    assert "user:password" not in listed["message"]
    assert "token=secret" not in listed["message"]
    assert listed["details"]["password"] == "[REDACTED]"
    assert listed["details"]["server_id"] == "server-safe"
    assert listed["details"]["event_code_compatibility"] == "legacy_event_type"
    technical_path = get_settings().paths.technical_log_dir / "event-contract-test.jsonl"
    technical_record = next(
        json.loads(line) for line in technical_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event_id") == written["event_id"]
    )
    assert technical_record["request_id"] == "request-top-level"
    assert technical_record["job_id"] == "job-top-level"
    assert technical_record["workflow_id"] == "workflow-top-level"
    assert technical_record["causation_id"] == "cause-top-level"
    operational_lines = get_settings().paths.operational_events_path.read_text(encoding="utf-8").splitlines()
    operational_record = next(
        json.loads(line) for line in operational_lines
        if json.loads(line).get("event_id") == operational["event_id"]
    )
    assert operational_record["request_id"] == "request-top-level"
    assert operational_record["job_id"] == "job-top-level"
    assert operational_record["details"]["event_code_compatibility"] == "legacy_event_type"
    with db_session() as connection:
        transactional = write_operational_log_in_connection(
            connection, event_type="transactional_legacy", message="legacy", level="info",
            component="events-test", details={},
        )
    assert transactional["details"]["event_code_compatibility"] == "legacy_event_type"


def test_operational_created_at_keeps_sqlite_timestamp_format_for_all_writers() -> None:
    legacy = write_operational_log(event_type="timestamp_legacy", message="legacy")
    typed = write_operational_event(
        severity="info", event_type="timestamp_typed", event_code="timestamp_typed", message="typed"
    )
    with db_session() as connection:
        rows = connection.execute(
            "SELECT event_id, created_at FROM operational_logs WHERE event_id IN (?, ?)",
            (legacy["event_id"], typed.event_id),
        ).fetchall()
    assert len(rows) == 2
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["created_at"]) for row in rows)


def test_direct_typed_writer_rejects_empty_event_code() -> None:
    with pytest.raises(ValueError, match="event_code"):
        write_operational_event(
            severity="info", event_type="typed", event_code="  ", message="typed",
        )


def test_typed_operational_category_is_independent_of_event_type_text() -> None:
    event = write_operational_event(
        severity="info", event_type="probe_failed_unfamiliar_label",
        event_code="core.routing.probe_outcome.changed", message="typed",
    )
    assert event.details["event_category"] == "operational"


def test_scrub_scans_unchanged_jsonl_without_creating_temporary_output(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "events.jsonl"
    lines = [
        json.dumps(
            {"event_id": f"event-{index}", "details": {"server_id": f"s-{index}"}},
            sort_keys=True,
        )
        for index in range(4)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = path.stat()
    scanned: list[object] = []
    actual_sanitize = event_contract.sanitize_value

    def record_full_scan(value, *, key=None):
        if isinstance(value, dict) and "event_id" in value:
            scanned.append(value)
        return actual_sanitize(value, key=key)

    def unexpected_temp(*_args, **_kwargs):
        raise AssertionError("unchanged JSONL must not create a temporary output")

    def unexpected_fsync(*_args, **_kwargs):
        raise AssertionError("unchanged JSONL must not fsync temporary output")

    monkeypatch.setattr(event_contract, "sanitize_value", record_full_scan)
    monkeypatch.setattr(event_contract.tempfile, "NamedTemporaryFile", unexpected_temp)
    monkeypatch.setattr(event_contract.os, "fsync", unexpected_fsync)

    result = scrub_jsonl_files([path])

    after = path.stat()
    assert result == {"files_rewritten": 0, "records_scanned": 4}
    assert len(scanned) == 4
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino, before.st_mode, before.st_mtime_ns
    )


def test_scrub_changed_jsonl_keeps_mode_malformed_lines_and_atomic_failure_contract(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(
        b'{"event_id":"event-1","details":{"password":"secret"}}\n'
        b'not-json token=secret\r\n \t\r\n\r\n'
    )
    path.chmod(0o640)
    original = path.read_bytes()
    actual_fsync = event_contract.os.fsync
    actual_replace = event_contract.os.replace

    def failed_fsync(_fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(event_contract.os, "fsync", failed_fsync)
    with pytest.raises(OSError, match="synthetic fsync failure"):
        scrub_jsonl_files([path])
    assert path.read_bytes() == original
    assert (path.stat().st_mode & 0o777) == 0o640
    assert list(tmp_path.glob("*.scrub.tmp")) == []
    monkeypatch.setattr(event_contract.os, "fsync", actual_fsync)

    def failed_replace(source, destination):
        assert Path(source).parent == path.parent
        assert Path(destination) == path
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(event_contract.os, "replace", failed_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        scrub_jsonl_files([path])
    assert path.read_bytes() == original
    assert (path.stat().st_mode & 0o777) == 0o640
    assert list(tmp_path.glob("*.scrub.tmp")) == []
    monkeypatch.setattr(event_contract.os, "replace", actual_replace)

    result = scrub_jsonl_files([path])
    rendered = path.read_text(encoding="utf-8")
    records = rendered.splitlines()
    assert result == {"files_rewritten": 1, "records_scanned": 1}
    assert json.loads(records[0])["details"]["password"] == "[REDACTED]"
    assert json.loads(records[1]) == "not-json token=[REDACTED]"
    assert records[2] == " \t"
    assert records[3] == ""
    assert (path.stat().st_mode & 0o777) == 0o640
    assert list(tmp_path.glob("*.scrub.tmp")) == []


@pytest.mark.parametrize("mutation", ["append", "replace"])
def test_scrub_does_not_overwrite_jsonl_changed_between_scan_and_rewrite(
    monkeypatch, tmp_path, mutation: str
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event_id":"event-1","details":{"password":"secret"}}\n'
        '{"event_id":"event-2","details":{"password":"secret"}}\n',
        encoding="utf-8",
    )
    replacement_bytes = b'{"event_id":"replacement","details":{"token":"new-secret"}}\n'
    actual_sanitize = event_contract.sanitize_value
    calls = 0

    def mutate_during_rewrite(value, *, key=None):
        nonlocal calls
        if isinstance(value, dict) and "event_id" in value:
            calls += 1
        if calls == 2:
            if mutation == "append":
                with path.open("a", encoding="utf-8") as stream:
                    stream.write('{"event_id":"late-append"}\n')
            else:
                replacement = tmp_path / "replacement.jsonl"
                replacement.write_bytes(replacement_bytes)
                event_contract.os.replace(replacement, path)
        return actual_sanitize(value, key=key)

    monkeypatch.setattr(event_contract, "sanitize_value", mutate_during_rewrite)
    with pytest.raises(OSError, match="changed during scrub"):
        scrub_jsonl_files([path])

    if mutation == "append":
        assert "late-append" in path.read_text(encoding="utf-8")
        assert "[REDACTED]" not in path.read_text(encoding="utf-8")
    else:
        assert path.read_bytes() == replacement_bytes
    assert list(tmp_path.glob("*.scrub.tmp")) == []


@pytest.mark.parametrize("mutation", ["append", "replace"])
def test_scrub_rejects_mutation_between_scan_and_atomic_rewrite(
    monkeypatch, tmp_path, mutation: str
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event_id":"event-1","details":{"password":"secret"}}\n',
        encoding="utf-8",
    )
    replacement_bytes = b'{"event_id":"replacement","details":{"token":"new-secret"}}\n'
    actual_temp = event_contract.tempfile.NamedTemporaryFile
    scanned = False
    actual_sanitize = event_contract.sanitize_value

    def mark_scanned(value, *, key=None):
        nonlocal scanned
        scanned = True
        return actual_sanitize(value, key=key)

    def mutate_before_rewrite(*args, **kwargs):
        if scanned:
            if mutation == "append":
                with path.open("a", encoding="utf-8") as stream:
                    stream.write('{"event_id":"late-append"}\n')
            else:
                replacement = tmp_path / "replacement.jsonl"
                replacement.write_bytes(replacement_bytes)
                event_contract.os.replace(replacement, path)
        return actual_temp(*args, **kwargs)

    monkeypatch.setattr(event_contract, "sanitize_value", mark_scanned)
    monkeypatch.setattr(event_contract.tempfile, "NamedTemporaryFile", mutate_before_rewrite)
    with pytest.raises(OSError, match="changed during scrub"):
        scrub_jsonl_files([path])

    if mutation == "append":
        assert "late-append" in path.read_text(encoding="utf-8")
    else:
        assert path.read_bytes() == replacement_bytes
    assert list(tmp_path.glob("*.scrub.tmp")) == []


@pytest.mark.parametrize("mutation", ["append", "mode"])
def test_scrub_rechecks_source_state_after_temp_fsync_before_replace(
    monkeypatch, tmp_path, mutation: str
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event_id":"event-1","details":{"password":"secret"}}\n',
        encoding="utf-8",
    )
    path.chmod(0o640)
    actual_fsync = event_contract.os.fsync
    changed = False

    def mutate_at_fsync(fd: int) -> None:
        nonlocal changed
        if not changed:
            changed = True
            if mutation == "append":
                with path.open("a", encoding="utf-8") as stream:
                    stream.write('{"event_id":"late-append"}\n')
            else:
                path.chmod(0o600)
        actual_fsync(fd)

    monkeypatch.setattr(event_contract.os, "fsync", mutate_at_fsync)
    with pytest.raises(OSError, match="changed during scrub"):
        scrub_jsonl_files([path])

    if mutation == "append":
        assert "late-append" in path.read_text(encoding="utf-8")
        assert (path.stat().st_mode & 0o777) == 0o640
    else:
        assert (path.stat().st_mode & 0o777) == 0o600
        assert "password" in path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.scrub.tmp")) == []
