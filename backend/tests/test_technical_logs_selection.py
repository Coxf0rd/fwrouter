from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

from fwrouter_api.services import logs


def _use_log_dir(monkeypatch, directory: Path) -> None:
    monkeypatch.setattr(
        logs,
        "get_settings",
        lambda: SimpleNamespace(
            paths=SimpleNamespace(technical_log_dir=directory),
        ),
    )


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )


def test_technical_log_top_k_preserves_equal_timestamp_order_and_filters(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "technical"
    log_dir.mkdir()
    timestamp = "2026-10-05T12:00:00+00:00"
    _write_events(log_dir / "a.jsonl", [
        {"event_id": "a-first", "timestamp": timestamp, "level": "error", "event_type": "wanted"},
        {"event_id": "a-second", "timestamp": timestamp, "level": "error", "event_type": "wanted"},
        {"event_id": "filtered-level", "timestamp": "2026-10-05T12:01:00+00:00", "level": "info", "event_type": "wanted"},
    ])
    _write_events(log_dir / "b.jsonl", [
        {"event_id": "b-third", "timestamp": timestamp, "level": "error", "event_type": "wanted"},
        {"event_id": "filtered-type", "timestamp": "2026-10-05T12:02:00+00:00", "level": "error", "event_type": "other"},
    ])
    _use_log_dir(monkeypatch, log_dir)

    rows = logs.list_technical_logs(limit=2, level="error", event_type="wanted")

    assert [row["event_id"] for row in rows] == ["a-first", "a-second"]
    assert [row["component"] for row in rows] == ["a", "a"]


def test_technical_log_order_matches_timestamp_parse_and_file_line_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "technical"
    log_dir.mkdir()
    _write_events(log_dir / "a.jsonl", [
        {"event_id": "a-equal-1", "timestamp": "2026-10-05T14:00:00+02:00"},
        {"event_id": "a-equal-2", "timestamp": "2026-10-05T13:00:00+01:00"},
        {"event_id": "a-invalid", "timestamp": "not-a-timestamp"},
        {"event_id": "a-missing"},
    ])
    _write_events(log_dir / "b.jsonl", [
        {"event_id": "b-newer", "timestamp": "2026-10-05T12:30:00+00:00"},
        {"event_id": "b-equal", "timestamp": "2026-10-05T11:00:00-01:00"},
        {"event_id": "b-invalid", "timestamp": "also-invalid"},
        {"event_id": "b-missing"},
    ])
    _use_log_dir(monkeypatch, log_dir)

    rows = logs.list_technical_logs(limit=20)

    assert [row["event_id"] for row in rows] == [
        "b-newer",
        "a-equal-1",
        "a-equal-2",
        "b-equal",
        "a-invalid",
        "a-missing",
        "b-invalid",
        "b-missing",
    ]


def test_technical_log_top_k_keeps_existing_malformed_file_boundary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "technical"
    log_dir.mkdir()
    (log_dir / "a.jsonl").write_text(
        '{"event_id":"before-error","timestamp":"2026-10-05T12:00:00+00:00"}\n'
        'not-json\n'
        '{"event_id":"after-error","timestamp":"2026-10-05T12:03:00+00:00"}\n',
        encoding="utf-8",
    )
    _write_events(log_dir / "b.jsonl", [
        {"event_id": "other-file", "timestamp": "2026-10-05T11:00:00+00:00"},
    ])
    _use_log_dir(monkeypatch, log_dir)

    rows = logs.list_technical_logs(limit=2)

    assert [row["event_id"] for row in rows] == ["before-error", "other-file"]


def test_technical_log_component_filter_uses_normalized_filename(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "technical"
    log_dir.mkdir()
    _write_events(log_dir / "some_path.jsonl", [
        {"event_id": "selected", "timestamp": "2026-10-05T12:00:00+00:00"},
    ])
    _write_events(log_dir / "other.jsonl", [
        {"event_id": "other", "timestamp": "2026-10-05T13:00:00+00:00"},
    ])
    _use_log_dir(monkeypatch, log_dir)

    rows = logs.list_technical_logs(component="some/path", limit=20)

    assert [row["event_id"] for row in rows] == ["selected"]
    assert logs.list_technical_logs(component="!!!", limit=20) == []


def test_technical_log_non_object_json_is_skipped(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "technical"
    log_dir.mkdir()
    (log_dir / "mixed-shape.jsonl").write_text(
        '[]\n{"event_id":"object","timestamp":"2026-10-05T12:00:00+00:00"}\n',
        encoding="utf-8",
    )
    _use_log_dir(monkeypatch, log_dir)

    rows = logs.list_technical_logs(limit=20)

    assert [row["event_id"] for row in rows] == ["object"]


def test_technical_log_top_k_sanitizes_selected_records_before_return(
    monkeypatch,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "technical"
    log_dir.mkdir()
    _write_events(log_dir / "security.jsonl", [
        {
            "event_id": "latest",
            "timestamp": "2026-10-05T12:00:00+00:00",
            "message": "Bearer raw-token",
            "details": {
                "password": "raw-password",
                "url": "https://user:secret@example.invalid/path?api_key=raw-key",
            },
        },
        {"event_id": "older", "timestamp": "2026-10-05T11:00:00+00:00"},
    ])
    _use_log_dir(monkeypatch, log_dir)
    sanitize_calls = 0
    original_sanitize = logs.sanitize_value

    def tracked_sanitize(value, *, key=None):
        nonlocal sanitize_calls
        if key is None:
            sanitize_calls += 1
        return original_sanitize(value, key=key)

    monkeypatch.setattr(logs, "sanitize_value", tracked_sanitize)

    rows = logs.list_technical_logs(limit=1)

    assert len(rows) == 1
    assert rows[0]["details"]["password"] == "[REDACTED]"
    assert "raw-token" not in rows[0]["message"]
    assert "raw-password" not in json.dumps(rows)
    assert "raw-key" not in json.dumps(rows)
    assert sanitize_calls == 1
