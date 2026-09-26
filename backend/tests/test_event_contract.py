from __future__ import annotations

import json
import re

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.events import write_operational_event
from fwrouter_api.core.config import get_settings
from fwrouter_api.services.event_contract import (
    MAX_EVENT_DETAILS_BYTES,
    normalize_event_details,
    reset_event_context,
    sanitize_value,
    set_event_context,
)
from fwrouter_api.services.logs import (
    list_technical_logs,
    write_operational_log,
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


def test_operational_created_at_keeps_sqlite_timestamp_format_for_all_writers() -> None:
    legacy = write_operational_log(event_type="timestamp_legacy", message="legacy")
    typed = write_operational_event(
        severity="info", event_type="timestamp_typed", message="typed"
    )
    with db_session() as connection:
        rows = connection.execute(
            "SELECT event_id, created_at FROM operational_logs WHERE event_id IN (?, ?)",
            (legacy["event_id"], typed.event_id),
        ).fetchall()
    assert len(rows) == 2
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["created_at"]) for row in rows)
