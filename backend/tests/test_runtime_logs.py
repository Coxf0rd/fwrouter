from __future__ import annotations
from _test_support import configure_test_state_dir as _configure_env

from pathlib import Path
from threading import Event, Thread
import json
import os

import pytest
import yaml

from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services import mihomo_reconcile as mihomo_reconcile_service
from fwrouter_api.services import (
    mihomo_reconcile_fingerprint as mihomo_reconcile_fingerprint_service,
)
from fwrouter_api.services.mihomo_reconcile_fingerprint import mihomo_udp_policy_receipt_matches
from fwrouter_api.services import xray as xray_service
from fwrouter_api.services import logs_retention
from fwrouter_api.services import jsonl_io
from fwrouter_api.services.jsonl_io import append_jsonl, jsonl_lease




def test_mihomo_reconcile_skip_writes_only_technical_log(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    runtime_incarnation = "pytest-runtime"
    monkeypatch.setattr(mihomo_reconcile_service, "_mihomo_incarnation", lambda: runtime_incarnation)
    monkeypatch.setattr(
        mihomo_reconcile_fingerprint_service,
        "_current_runtime_incarnation",
        lambda: runtime_incarnation,
    )
    unit_native_udp_proof = {
        "ok": True,
        "native_top_level_rules_match": True,
        "selector_membership_matches": True,
        "guarded_rules_match": True,
    }
    monkeypatch.setattr(
        mihomo_reconcile_service,
        "_native_udp_policy_proof",
        lambda: dict(unit_native_udp_proof),
    )
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
    base_path = Path(mihomo_config_service._resolved_base_config_path())
    candidate_path = Path(mihomo_config_service._resolved_candidate_config_path())
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(yaml.safe_dump(candidate_config, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr(mihomo_config_service, "write_operational_log", _op)
    monkeypatch.setattr(mihomo_config_service, "write_technical_log", _tech)
    monkeypatch.setattr(mihomo_config_service, "get_mihomo_config_status", lambda **_kwargs: {
        "base_config": candidate_config,
        "candidate_config": candidate_config,
        "base_path": str(base_path),
        "candidate_path": str(candidate_path),
        "base_exists": base_path.is_file(),
        "candidate_exists": candidate_path.is_file(),
    })
    candidate_generation_calls: list[str] = []

    def _fake_write_mihomo_candidate_config(routing=None):
        candidate_generation_calls.append("write")
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(yaml.safe_dump(candidate_config, sort_keys=False), encoding="utf-8")
        return {
            "candidate_path": str(candidate_path),
            "rules": candidate_config["rules"],
            "handoff_assignments": [],
            "resolved_selective_default": "direct",
            "final_match_rule": "MATCH,DIRECT",
            "transparent_final_match_rule": "MATCH,DIRECT",
            "config": candidate_config,
        }

    monkeypatch.setattr(
        mihomo_config_service,
        "write_mihomo_candidate_config",
        _fake_write_mihomo_candidate_config,
    )
    restart_calls: list[str] = []
    monkeypatch.setattr(
        mihomo_config_service,
        "restart_mihomo_container",
        lambda **kwargs: restart_calls.append(str(kwargs.get("action") or "restart")),
    )
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
    assert result["reconcile_reason"] == "unchanged_config"
    assert candidate_generation_calls == ["write"]
    assert restart_calls == []
    assert written_operational == []
    assert written_technical[-1]["event_type"] == "mihomo_reconcile_skipped"
    assert mihomo_udp_policy_receipt_matches() is True


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


def test_log_retention_no_expiry_skips_rewrite(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        logs_retention,
        "_utc_now",
        lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc),
    )
    path = tmp_path / "events.jsonl"
    original = '{"timestamp":"2026-10-05T12:00:00+00:00","event_type":"runtime_failed","event_category":"operational","id":"first"}\n\n'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(logs_retention, "replace_jsonl", lambda *args, **kwargs: pytest.fail("no-expiry must not rewrite"))

    result = logs_retention._cleanup_jsonl_file(
        path,
        timestamp_field="timestamp",
        retention_days=30,
        dry_run=False,
    )

    assert result["total_lines"] == 2
    assert result["kept_lines"] == 1
    assert result["deleted_lines"] == 0
    assert result["invalid_lines"] == 0
    assert result["rewritten"] is False
    assert path.read_text(encoding="utf-8") == original


def test_log_retention_expired_rewrite_preserves_order_and_malformed_lines(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"timestamp":"2020-01-01T00:00:00+00:00","event_type":"runtime_failed","event_category":"operational","id":"expired"}\n'
        'not-json\n'
        '\n'
        '{"timestamp":"2026-10-05T12:00:00+00:00","event_type":"runtime_failed","event_category":"operational","id":"newer"}\n'
        '{"event_type":"runtime_failed","event_category":"operational","id":"missing-timestamp"}\n',
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
        '{"timestamp":"2026-10-05T12:00:00+00:00","event_type":"runtime_failed","event_category":"operational","id":"newer"}\n'
        '{"event_type":"runtime_failed","event_category":"operational","id":"missing-timestamp"}\n'
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
    source = '{"timestamp":"2020-01-01T00:00:00+00:00","event_type":"runtime_failed","event_category":"operational"}\nnot-json\n'
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


def test_log_retention_rewrite_serializes_with_concurrent_appender(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"timestamp":"2020-01-01T00:00:00+00:00","event_type":"runtime_failed","event_category":"operational"}\n', encoding="utf-8")
    monkeypatch.setattr(
        logs_retention,
        "_utc_now",
        lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc),
    )
    writer_started = Event()
    writer_errors: list[BaseException] = []
    writer_threads: list[Thread] = []
    original_replace = logs_retention.replace_jsonl

    def _start_writer_before_replace(lease, lines, *, expected):
        def write():
            writer_started.set()
            try:
                append_jsonl(path, '{"timestamp":"2026-10-05T12:00:00+00:00","event_type":"runtime_failed","event_category":"operational","id":"appended"}\n')
            except BaseException as exc:
                writer_errors.append(exc)
        thread = Thread(target=write)
        writer_threads.append(thread)
        thread.start()
        assert writer_started.wait(timeout=1)
        return original_replace(lease, lines, expected=expected)

    monkeypatch.setattr(logs_retention, "replace_jsonl", _start_writer_before_replace)

    result = logs_retention._cleanup_jsonl_file(
        path,
        timestamp_field="timestamp",
        retention_days=30,
        dry_run=False,
    )
    for thread in writer_threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert result["deleted_lines"] == 1
    assert result["rewritten"] is True
    assert writer_errors == [], repr(writer_errors)
    content = path.read_text(encoding="utf-8")
    assert '"id":"appended"' in content
    assert "2020-01-01" not in content


def test_concurrent_retention_cleaners_serialize_on_same_jsonl_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logs_retention, "_utc_now", lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc))
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"timestamp":"2020-01-01T00:00:00+00:00","event_type":"runtime_failed","event_category":"operational","id":"expired"}\n'
        '{"timestamp":"2026-10-05T12:00:00+00:00","event_type":"runtime_failed","event_category":"operational","id":"fresh"}\n',
        encoding="utf-8",
    )
    start = Event()
    results: list[dict] = []
    errors: list[BaseException] = []

    def clean():
        start.wait(timeout=1)
        try:
            results.append(logs_retention._cleanup_jsonl_file(
                path, timestamp_field="timestamp", retention_days=30, dry_run=False,
            ))
        except BaseException as exc:
            errors.append(exc)

    threads = [Thread(target=clean), Thread(target=clean)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert errors == []
    assert sum(result["deleted_lines"] for result in results) == 1
    assert '"id":"expired"' not in path.read_text(encoding="utf-8")
    assert '"id":"fresh"' in path.read_text(encoding="utf-8")


def test_log_retention_failure_preserves_source_and_removes_private_temp(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    source = (
        '{"timestamp":"2020-01-01T00:00:00+00:00","event_type":"runtime_failed","event_category":"operational"}\n'
        '{"timestamp":"2026-10-05T12:00:00+00:00","event_type":"runtime_failed","event_category":"operational"}\n'
    )
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        logs_retention,
        "_utc_now",
        lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc),
    )
    source = path.read_text(encoding="utf-8")
    def _fail_replace(*args, **kwargs):
        raise OSError("isolated replace failure")

    monkeypatch.setattr(jsonl_io.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="isolated replace failure"):
        logs_retention._cleanup_jsonl_file(
            path,
            timestamp_field="timestamp",
            retention_days=30,
            dry_run=False,
        )

    assert path.read_text(encoding="utf-8") == source
    assert list(tmp_path.glob(".*.rewrite-*.tmp")) == []


def test_log_retention_holds_audit_security_unknown_and_malformed_rows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logs_retention, "_utc_now", lambda: logs_retention.datetime(2026, 10, 5, 12, tzinfo=logs_retention.timezone.utc))
    path = tmp_path / "events.jsonl"
    rows = [
        {"timestamp": "2020-01-01T00:00:00+00:00", "event_type": "client.alias_changed", "event_code": "client.alias_changed", "event_category": "audit", "id": "audit"},
        {"timestamp": "2020-01-01T00:00:00+00:00", "event_type": "mutation_denied", "event_code": "client_list.mutation_denied", "event_category": "operational", "id": "deny"},
        {"timestamp": "2020-01-01T00:00:00+00:00", "event_type": "runtime_failed", "event_category": "not-a-category", "id": "bad-category"},
        {"timestamp": "2020-01-01T00:00:00+00:00", "event_type": "client.alias_changed", "event_code": "client.alias_changed", "event_category": "operational", "details": {"event_category": "audit"}, "id": "conflicting-audit-markers"},
        {"timestamp": "2020-01-01T00:00:00+00:00", "event_type": "runtime_failed", "event_category": "operational", "details": "malformed-details", "id": "non-object-details"},
        {"timestamp": "2020-01-01T00:00:00+00:00", "event_type": "runtime_failed", "event_category": "operational", "id": "ordinary"},
    ]
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows) + "truncated", encoding="utf-8")

    result = logs_retention._cleanup_jsonl_file(path, timestamp_field="timestamp", retention_days=30, dry_run=False)

    content = path.read_text(encoding="utf-8")
    assert '"id":"audit"' in content
    assert '"id":"deny"' in content
    assert '"id":"bad-category"' in content
    assert '"id":"conflicting-audit-markers"' in content
    assert '"id":"non-object-details"' in content
    assert '"id":"ordinary"' not in content
    assert content.endswith("truncated")
    assert result["held_lines"] == 5
    assert result["deleted_lines"] == 1
    assert result["invalid_lines"] == 1


def test_jsonl_append_rejects_symlink_and_tightens_legacy_mode(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o644)
    append_jsonl(target, "new\n")
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text(encoding="utf-8") == "old\nnew\n"

    outside = tmp_path / "outside.jsonl"
    outside.write_text("outside\n", encoding="utf-8")
    symlink = tmp_path / "linked.jsonl"
    symlink.symlink_to(outside)
    with pytest.raises(OSError):
        append_jsonl(symlink, "must-not-write\n")
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_jsonl_append_parent_substitution_after_validation_stays_fd_anchored(
    monkeypatch, tmp_path: Path,
) -> None:
    parent = tmp_path / "logs"
    parent.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    target = parent / "events.jsonl"
    target.write_text("original\n", encoding="utf-8")
    (outside / target.name).write_text("outside\n", encoding="utf-8")
    moved_parent = tmp_path / "moved-logs"
    actual_proc_path = jsonl_io._proc_path
    substituted = False

    def substitute_before_open(directory_fd: int, name: str) -> Path:
        nonlocal substituted
        if name == target.name and not substituted:
            substituted = True
            parent.rename(moved_parent)
            parent.symlink_to(outside, target_is_directory=True)
        return actual_proc_path(directory_fd, name)

    monkeypatch.setattr(jsonl_io, "_proc_path", substitute_before_open)
    with pytest.raises(OSError, match="substituted"):
        append_jsonl(target, "append-attempt\n")
    assert (outside / target.name).read_text(encoding="utf-8") == "outside\n"
    assert (moved_parent / target.name).read_text(encoding="utf-8") == "original\n"


def test_jsonl_replace_parent_substitution_uses_owned_directory_fd(
    monkeypatch, tmp_path: Path,
) -> None:
    parent = tmp_path / "logs"
    parent.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    target = parent / "events.jsonl"
    target.write_text("old\n", encoding="utf-8")
    (outside / target.name).write_text("outside\n", encoding="utf-8")
    moved_parent = tmp_path / "moved-logs"
    actual_replace = jsonl_io.os.replace
    actual_validate_parent = jsonl_io._validate_parent
    validations = 0

    with jsonl_lease(target) as lease:
        expected = jsonl_io._target_info(lease)
        assert expected is not None

        def substitute_before_replace(source, destination, **kwargs) -> None:
            actual_replace(source, destination, **kwargs)

        def rename_after_final_validation(lease) -> None:
            nonlocal validations
            actual_validate_parent(lease)
            validations += 1
            if validations == 3:
                parent.rename(moved_parent)
                parent.symlink_to(outside, target_is_directory=True)

        monkeypatch.setattr(jsonl_io.os, "replace", substitute_before_replace)
        monkeypatch.setattr(jsonl_io, "_validate_parent", rename_after_final_validation)
        jsonl_io.replace_jsonl(lease, iter(["new\n"]), expected=expected)

    assert (outside / target.name).read_text(encoding="utf-8") == "outside\n"
    assert (moved_parent / target.name).read_text(encoding="utf-8") == "new\n"


def test_jsonl_lease_rejects_substituted_lock_and_orphan_cleanup_uses_lease(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("safe\n", encoding="utf-8")
    lock_path = tmp_path / ".events.jsonl.lock"
    lock_path.symlink_to(path)
    with pytest.raises(OSError):
        with jsonl_lease(path):
            pass
    lock_path.unlink()
    with jsonl_lease(path):
        with pytest.raises(TimeoutError):
            with jsonl_lease(path, timeout_seconds=0.01):
                pass

    orphan = tmp_path / ".events.jsonl.rewrite-123-deadbeefdeadbeef.tmp"
    orphan.write_text("private temp", encoding="utf-8")
    legacy_source = tmp_path / "legacy.jsonl"
    legacy_source.write_text("source\n", encoding="utf-8")
    legacy_orphan = tmp_path / "legacy.tmp"
    legacy_orphan.write_text("legacy temp", encoding="utf-8")
    old_scrub_orphan = tmp_path / ".events.jsonl.abcd1234.scrub.tmp"
    old_scrub_orphan.write_text("old scrub temp", encoding="utf-8")
    unknown_orphan = tmp_path / "notes.tmp"
    unknown_orphan.write_text("unrelated temp", encoding="utf-8")
    similarly_named = tmp_path / ".events.jsonl.unowned.tmp"
    similarly_named.write_text("unrelated hidden temp", encoding="utf-8")
    old = logs_retention.datetime.now(logs_retention.timezone.utc).timestamp() - 3600
    for stale_path in (orphan, legacy_orphan, old_scrub_orphan, unknown_orphan, similarly_named):
        os.utime(stale_path, (old, old))
    result = logs_retention._cleanup_orphan_tmp_files(tmp_path, retention_minutes=15, dry_run=False)
    assert result["deleted_count"] == 3
    assert not orphan.exists()
    assert not legacy_orphan.exists()
    assert not old_scrub_orphan.exists()
    assert unknown_orphan.exists()
    assert similarly_named.exists()
