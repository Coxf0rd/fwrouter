from __future__ import annotations
from _test_support import configure_test_state_dir as _configure_env

import json
from pathlib import Path

import pytest

from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services import xray as xray_service
from fwrouter_api.services import logs_retention




def test_mihomo_reconcile_skip_writes_only_technical_log(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    monkeypatch.setattr("fwrouter_api.services.mihomo_reconcile._mihomo_incarnation", lambda: "pytest-runtime")
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])

    written_operational: list[dict] = []
    written_technical: list[dict] = []

    def _op(**kwargs):
        written_operational.append(kwargs)
        return kwargs

    def _tech(**kwargs):
        written_technical.append(kwargs)
        return kwargs

    candidate_config = mihomo_config_service.build_mihomo_config({"selective_default": "direct"})
    monkeypatch.setattr(mihomo_config_service, "write_operational_log", _op)
    monkeypatch.setattr(mihomo_config_service, "write_technical_log", _tech)
    monkeypatch.setattr(mihomo_config_service, "get_mihomo_config_status", lambda: {
        "base_config": candidate_config,
        "candidate_config": candidate_config,
    })
    monkeypatch.setattr(mihomo_config_service, "write_mihomo_candidate_config", lambda routing=None: {
        "candidate_path": "candidate",
        "rules": candidate_config["rules"],
        "handoff_assignments": [],
        "resolved_selective_default": "direct",
        "final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,DIRECT",
        "config": candidate_config,
    })
    monkeypatch.setattr(mihomo_config_service, "validate_mihomo_candidate_config", lambda routing=None: {
        "ok": True,
        "resolved_selective_default": "direct",
        "final_match_rule": "MATCH,DIRECT",
        "expected_final_match_rule": "MATCH,DIRECT",
        "transparent_final_match_rule": "MATCH,DIRECT",
        "expected_transparent_final_match_rule": "MATCH,DIRECT",
        "state_consistency_ok": True,
        "transparent_state_consistency_ok": True,
    })

    result = mihomo_config_service.reconcile_mihomo_runtime({"selective_default": "direct"})

    assert result["ok"] is True
    assert written_operational == []
    assert written_technical[-1]["event_type"] == "mihomo_reconcile_skipped"


def test_xray_materialize_failure_writes_both_log_types(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    written_operational: list[dict] = []
    written_technical: list[dict] = []

    monkeypatch.setattr(xray_service, "write_operational_log", lambda **kwargs: written_operational.append(kwargs) or kwargs)
    monkeypatch.setattr(xray_service, "write_technical_log", lambda **kwargs: written_technical.append(kwargs) or kwargs)
    monkeypatch.setattr(xray_service, "collect_xray_runtime_bindings", lambda: [])
    monkeypatch.setattr(xray_service, "_write_xray_bindings_state", lambda bindings, applied_ok=False: {"ok": applied_ok})

    class _Result:
        ok = False
        message = "apply failed"
        error_code = "XRAY_BINDINGS_APPLY_FAILED"
        details = {"stage": "reload"}

    class _Adapter:
        def materialize_client_bindings(
            self, bindings, *, force_reload: bool = False, client_modes: dict[str, str] | None = None
        ):
            return _Result()

    monkeypatch.setattr(xray_service, "DEFAULT_XRAY_ADAPTER", _Adapter())

    result = xray_service.materialize_xray_runtime_bindings(
        requested_by="pytest",
        prepare_mihomo_handoff=False,
    )

    assert result["ok"] is False
    assert written_operational[-1]["event_type"] == "xray_binding_materialization_failed"
    assert written_technical[-1]["event_type"] == "xray_binding_materialization_failed"


def test_log_retention_no_expiry_skips_temp_rewrite_and_preserves_append(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        logs_retention,
        "_utc_now",
        lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc),
    )
    path = tmp_path / "events.jsonl"
    original = '{"timestamp":"2026-10-05T12:00:00+00:00","id":"first"}\n\n'
    appended = '{"timestamp":"2026-10-05T12:00:00+00:00","id":"concurrent"}\n'
    path.write_text(original, encoding="utf-8")
    temp_path = path.with_suffix(".tmp")
    opened_temp_for_write: list[Path] = []
    original_open = Path.open

    def _tracked_open(self, mode="r", *args, **kwargs):
        if self == temp_path and any(flag in mode for flag in "wax+"):
            opened_temp_for_write.append(self)
        return original_open(self, mode, *args, **kwargs)

    original_parse_timestamp = logs_retention._parse_timestamp
    appended_once = False

    def _append_while_scanning(value):
        nonlocal appended_once
        if not appended_once:
            appended_once = True
            with original_open(path, "a", encoding="utf-8") as handle:
                handle.write(appended)
        return original_parse_timestamp(value)

    monkeypatch.setattr(Path, "open", _tracked_open)
    monkeypatch.setattr(logs_retention, "_parse_timestamp", _append_while_scanning)

    result = logs_retention._cleanup_jsonl_file(
        path,
        timestamp_field="timestamp",
        retention_days=30,
        dry_run=False,
    )

    assert result["total_lines"] == 3
    assert result["kept_lines"] == 2
    assert result["deleted_lines"] == 0
    assert result["invalid_lines"] == 0
    assert result["rewritten"] is False
    assert path.read_text(encoding="utf-8") == original + appended
    assert opened_temp_for_write == []
    assert not temp_path.exists()


def test_log_retention_expired_rewrite_preserves_order_and_malformed_lines(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"timestamp":"2020-01-01T00:00:00+00:00","id":"expired"}\n'
        'not-json\n'
        '\n'
        '{"timestamp":"2026-10-05T12:00:00+00:00","id":"newer"}\n'
        '{"id":"missing-timestamp"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        logs_retention,
        "_utc_now",
        lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc),
    )

    result = logs_retention._cleanup_jsonl_file(
        path,
        timestamp_field="timestamp",
        retention_days=30,
        dry_run=False,
    )

    assert path.read_text(encoding="utf-8") == (
        "not-json\n"
        '{"timestamp":"2026-10-05T12:00:00+00:00","id":"newer"}\n'
        '{"id":"missing-timestamp"}\n'
    )
    assert result["total_lines"] == 5
    assert result["kept_lines"] == 3
    assert result["deleted_lines"] == 1
    assert result["invalid_lines"] == 2
    assert result["rewritten"] is True


def test_log_retention_expired_dry_run_counts_without_rewriting(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    source = '{"timestamp":"2020-01-01T00:00:00+00:00"}\nnot-json\n'
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        logs_retention,
        "_utc_now",
        lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc),
    )

    result = logs_retention._cleanup_jsonl_file(
        path,
        timestamp_field="timestamp",
        retention_days=30,
        dry_run=True,
    )

    assert path.read_text(encoding="utf-8") == source
    assert result["total_lines"] == 2
    assert result["kept_lines"] == 1
    assert result["deleted_lines"] == 1
    assert result["invalid_lines"] == 1
    assert result["rewritten"] is False


def test_log_retention_does_not_replace_when_expiry_disappears_before_rewrite(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"timestamp":"2020-01-01T00:00:00+00:00"}\n', encoding="utf-8")
    monkeypatch.setattr(
        logs_retention,
        "_utc_now",
        lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc),
    )
    fresh_source = '{"timestamp":"2026-10-05T12:00:00+00:00","id":"rotated"}\n'
    original_open = Path.open
    source_read_count = 0

    def _rotate_between_scans(self, mode="r", *args, **kwargs):
        nonlocal source_read_count
        if self == path and mode == "r":
            source_read_count += 1
            if source_read_count == 2:
                path.write_text(fresh_source, encoding="utf-8")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _rotate_between_scans)

    result = logs_retention._cleanup_jsonl_file(
        path,
        timestamp_field="timestamp",
        retention_days=30,
        dry_run=False,
    )

    assert result["deleted_lines"] == 0
    assert result["rewritten"] is False
    assert path.read_text(encoding="utf-8") == fresh_source
    assert not path.with_suffix(".tmp").exists()


def test_log_retention_cleans_temp_file_and_closes_it_after_write_error(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    source = (
        '{"timestamp":"2020-01-01T00:00:00+00:00"}\n'
        '{"timestamp":"2026-10-05T12:00:00+00:00"}\n'
    )
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        logs_retention,
        "_utc_now",
        lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc),
    )
    temp_path = path.with_suffix(".tmp")
    original_open = Path.open
    opened_handles = []

    class _FailingWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            result = self.handle.__exit__(exc_type, exc, traceback)
            self.closed = self.handle.closed
            return result

        def write(self, value):
            self.handle.write(value)
            raise OSError("isolated write failure")

    def _fail_temp_write(self, mode="r", *args, **kwargs):
        if self == temp_path and mode == "w":
            handle = original_open(self, mode, *args, **kwargs)
            writer = _FailingWriter(handle)
            opened_handles.append(writer)
            return writer
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _fail_temp_write)

    with pytest.raises(OSError, match="isolated write failure"):
        logs_retention._cleanup_jsonl_file(
            path,
            timestamp_field="timestamp",
            retention_days=30,
            dry_run=False,
        )

    assert path.read_text(encoding="utf-8") == source
    assert len(opened_handles) == 1
    assert opened_handles[0].closed is True
    assert not temp_path.exists()
