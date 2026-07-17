from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path
from types import SimpleNamespace

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.modules import get_module_state, set_module_desired_state
from fwrouter_api.services.runtime_convergence import (
    _reset_runtime_convergence_state_for_tests,
    run_runtime_convergence_check,
)
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.traffic import record_traffic_samples
from fwrouter_api.services.watchdog import (
    detect_recent_vpn_traffic_attempts,
    run_vpn_watchdog_check,
    run_vpn_watchdog_auto_check,
    start_watchdog_scheduler,
    stop_watchdog_scheduler,
)


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    clear_live_probe_cache()
    _reset_runtime_convergence_state_for_tests()
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_last_runtime_convergence_status",
        lambda **kwargs: {
            "ok": True,
            "status": "ok",
            "checked": True,
            "repaired": False,
            "dnsmasq": {"ok": True, "restart_required": False},
            "dataplane": {"ok": True, "action": "none", "drift_detected": False},
        },
    )


def _seed_subject(subject_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                runtime_state,
                is_active
            )
            VALUES (?, 'lan', ?, ?, 'global', 'active', 1)
            """,
            (subject_id, subject_id, subject_id),
        )


def _set_global_vpn_auto(active_auto_server_id: str = "srv-1") -> None:
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES (?, ?, 'pytest', 'active')
            """,
            (active_auto_server_id, active_auto_server_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO server_preferences (server_id, vpn_auto, global_list) VALUES (?, 1, 1)",
            (active_auto_server_id,),
        )
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'vpn',
                applied_mode = 'vpn',
                server_mode = 'auto',
                active_auto_server_id = ?,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (active_auto_server_id,),
        )


def _record_vpn_activity(subject_id: str) -> None:
    record_traffic_samples(
        [
            {
                "counter_key": f"{subject_id}:vpn",
                "subject_id": subject_id,
                "path": "vpn",
                "rx_bytes": 100,
                "tx_bytes": 50,
            }
        ],
        collector="pytest",
        dry_run=False,
    )
    record_traffic_samples(
        [
            {
                "counter_key": f"{subject_id}:vpn",
                "subject_id": subject_id,
                "path": "vpn",
                "rx_bytes": 150,
                "tx_bytes": 80,
            }
        ],
        collector="pytest",
        dry_run=False,
    )


def test_detect_recent_vpn_traffic_attempts_uses_deltas(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-traffic")
    _record_vpn_activity("lan-traffic")

    signal = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert signal["observed"] is True
    assert signal["active_samples_count"] >= 1
    assert signal["checked_samples_count"] >= 1
    assert any(sample["activity_observed"] for sample in signal["samples"])


def test_watchdog_auto_check_pauses_when_global_mode_is_not_vpn(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()

    set_module_desired_state("watchdog", "enabled", run_now=False)

    result = run_vpn_watchdog_auto_check()
    module = get_module_state("watchdog")

    assert result["ok"] is True
    assert result["status"] == "paused_not_vpn"
    assert module is not None
    assert module["runtime_state"] == "paused"


def test_watchdog_auto_check_marks_module_running_on_healthy_path(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-vpn")
    _set_global_vpn_auto("srv-healthy")
    _record_vpn_activity("lan-vpn")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(health=lambda: SimpleNamespace(active_server_id="srv-healthy")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-healthy"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.check_active_server_delay",
        lambda **kwargs: {
            "ok": True,
            "server_id": "srv-healthy",
            "status": "success",
            "last_ping_ms": 42,
            "latency_label": "42 ms",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        },
    )

    result = run_vpn_watchdog_auto_check(
        allow_switch=False,
        traffic_window_seconds=300,
    )
    module = get_module_state("watchdog")

    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert result["traffic_signal"]["observed"] is True
    assert module is not None
    assert module["runtime_state"] == "running"


def test_watchdog_auto_check_reuses_fresh_successful_active_ping(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-cached")
    _set_global_vpn_auto("srv-cached")
    _record_vpn_activity("lan-cached")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO server_ping_state (
                server_id,
                status,
                last_ping_ms,
                checked_at,
                checked_by
            )
            VALUES ('srv-cached', 'success', 33, CURRENT_TIMESTAMP, 'pytest')
            """
        )

    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(health=lambda: SimpleNamespace(active_server_id="srv-cached")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-cached"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.check_active_server_delay",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("fresh ping must be reused")),
    )

    result = run_vpn_watchdog_auto_check(
        allow_switch=False,
        traffic_window_seconds=300,
    )

    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert result["active_check"]["cached"] is True
    assert result["active_check"]["last_ping_ms"] == 33


def test_watchdog_auto_check_marks_module_degraded_on_fail_open(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-fail")
    _set_global_vpn_auto("srv-fail")
    _record_vpn_activity("lan-fail")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(health=lambda: SimpleNamespace(active_server_id="srv-fail")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-fail"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.check_active_server_delay",
        lambda **kwargs: {
            "ok": False,
            "server_id": "srv-fail",
            "status": "failed",
            "last_ping_ms": None,
            "latency_label": "n/a",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": "PING_FAILED",
            "error_message": "ping failed",
            "updated_state": kwargs.get("update_state", False),
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.select_vpn_auto_server",
        lambda **kwargs: {
            "ok": False,
            "reason": kwargs.get("reason"),
            "apply": kwargs.get("apply", False),
            "selected_server_id": None,
            "selected_server_name": None,
            "fail_open_direct_recommended": True,
        },
    )

    result = run_vpn_watchdog_auto_check(
        allow_switch=True,
        traffic_window_seconds=300,
    )
    module = get_module_state("watchdog")

    assert result["ok"] is False
    assert result["status"] == "fail_open_direct_recommended"
    assert module is not None
    assert module["runtime_state"] == "degraded"
    assert module["error_code"] == "WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED"


def test_watchdog_auto_check_suppresses_switching_without_fresh_signal(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-stale")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(health=lambda: SimpleNamespace(active_server_id="srv-stale")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-stale"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )

    result = run_vpn_watchdog_auto_check(
        allow_switch=True,
        traffic_window_seconds=300,
    )
    module = get_module_state("watchdog")

    assert result["ok"] is True
    assert result["status"] == "paused_signal_unavailable"
    assert result["allow_switch"] is False
    assert result["traffic_signal"]["authoritative"] is False
    assert module is not None
    assert module["runtime_state"] == "degraded"
    assert module["error_code"] == "WATCHDOG_SIGNAL_UNAVAILABLE"


def test_watchdog_operational_log_does_not_use_server_id_as_subject(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-logged")

    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(health=lambda: SimpleNamespace(active_server_id="srv-logged")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-logged"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.check_active_server_delay",
        lambda **kwargs: {
            "ok": True,
            "server_id": "srv-logged",
            "status": "success",
            "last_ping_ms": 42,
            "latency_label": "42 ms",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        },
    )

    result = run_vpn_watchdog_check(
        traffic_attempts_observed=True,
        allow_switch=False,
        log_events=True,
    )

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT subject_id
            FROM operational_logs
            WHERE event_type = 'vpn_watchdog_healthy'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert result["ok"] is True
    assert row is not None
    assert row["subject_id"] is None


def test_start_watchdog_scheduler_respects_enabled_config(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_SCHEDULER_ENABLED", "true")
    get_settings.cache_clear()
    initialize_database()

    started = start_watchdog_scheduler()

    assert started is True
    stop_watchdog_scheduler()


def test_watchdog_reports_signal_unavailable_when_traffic_timer_missing(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-stale")
    set_module_desired_state("watchdog", "enabled", run_now=False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-stale"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": False,
            "authoritative": False,
            "safe_for_watchdog_auto": False,
            "last_collected_at": None,
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert result["status"] == "paused_signal_unavailable"
    assert result["module"]["error_code"] == "WATCHDOG_SIGNAL_UNAVAILABLE"


def test_watchdog_needs_initial_auto_selection_when_active_auto_missing(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'vpn',
                applied_mode = 'vpn',
                server_mode = 'auto',
                active_auto_server_id = NULL,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )
    set_module_desired_state("watchdog", "enabled", run_now=False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": False, "active_auto_server_id": None},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )

    result = run_vpn_watchdog_auto_check(allow_switch=False, traffic_window_seconds=300)

    assert result["ok"] is True
    assert result["status"] == "needs_initial_auto_selection"
    assert result["module"]["error_code"] == "WATCHDOG_INITIAL_AUTO_SELECTION_REQUIRED"


def test_watchdog_does_not_switch_on_idle_when_active_is_valid(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-idle")
    set_module_desired_state("watchdog", "enabled", run_now=False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-idle"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": False,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": "2026-06-29T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(health=lambda: SimpleNamespace(active_server_id="srv-idle")),
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert result["ok"] is True
    assert result["status"] == "no_failure_no_traffic"


def test_watchdog_auto_check_runs_for_scoped_vpn_subjects_even_when_global_mode_direct(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('srv-scoped', 'srv-scoped', 'pytest', 'active')
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO server_preferences (server_id, vpn_auto, global_list) VALUES ('srv-scoped', 1, 1)"
        )
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'direct',
                applied_mode = 'direct',
                server_mode = 'auto',
                active_auto_server_id = 'srv-scoped',
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )
    set_module_desired_state("watchdog", "enabled", run_now=False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-scoped"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: True,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": True,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": "2026-07-01T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.DEFAULT_MIHOMO_ADAPTER",
        SimpleNamespace(health=lambda: SimpleNamespace(active_server_id="srv-scoped")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.check_active_server_delay",
        lambda **kwargs: {
            "ok": True,
            "server_id": "srv-scoped",
            "status": "success",
            "last_ping_ms": 25,
            "latency_label": "25 ms",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=False, traffic_window_seconds=300)

    assert result["ok"] is True
    assert result["status"] == "healthy"
    assert result["traffic_signal"]["observed"] is True


def test_runtime_convergence_service_repairs_dnsmasq_for_scoped_selective(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state
            )
            VALUES ('srv-dns-converge', 'srv-dns-converge', 'pytest', 'active')
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO server_preferences (server_id, vpn_auto, global_list) VALUES ('srv-dns-converge', 1, 1)"
        )
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'direct',
                applied_mode = 'direct',
                server_mode = 'auto',
                active_auto_server_id = 'srv-dns-converge',
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )
    set_module_desired_state("watchdog", "enabled", run_now=False)

    calls = {"dnsmasq": 0, "dataplane": 0}
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence._compute_has_scoped_vpn_subjects",
        lambda: True,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_dnsmasq_rules",
        lambda: calls.__setitem__("dnsmasq", calls["dnsmasq"] + 1)
        or {
            "ok": True,
            "restart_required": True,
            "restart_reason": "nftset_probe_unhealthy",
            "message": "repaired",
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_current_routing_if_drift",
        lambda **kwargs: calls.__setitem__("dataplane", calls["dataplane"] + 1)
        or {"ok": True, "action": "none", "drift_detected": False},
    )

    result = run_runtime_convergence_check(
        requested_by="pytest",
        log_events=False,
        force=True,
    )

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["repaired"] is True
    assert calls == {"dnsmasq": 1, "dataplane": 1}


def test_watchdog_marks_module_degraded_when_runtime_convergence_is_unhealthy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-converge-fail")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.get_last_runtime_convergence_status",
        lambda **kwargs: {
            "ok": False,
            "status": "failed",
            "error_code": "DNSMASQ_SELECTIVE_CONTRACT_INCOMPLETE",
            "error_message": "nftset probe failed",
            "dnsmasq": {"ok": False},
            "dataplane": {"ok": True},
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)
    module = get_module_state("watchdog")

    assert result["ok"] is False
    assert result["status"] == "runtime_convergence_failed"
    assert result["runtime_convergence"]["error_code"] == "DNSMASQ_SELECTIVE_CONTRACT_INCOMPLETE"
    assert module is not None
    assert module["runtime_state"] == "degraded"
    assert module["error_code"] == "DNSMASQ_SELECTIVE_CONTRACT_INCOMPLETE"
