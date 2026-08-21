from __future__ import annotations
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import initialize_database


import json
from pathlib import Path
from datetime import datetime, timezone

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services.logs import list_technical_logs
from fwrouter_api.services.modules import get_module_state, set_module_desired_state
from fwrouter_api.services.runtime_convergence import (
    _reset_runtime_convergence_state_for_tests,
    run_runtime_convergence_check,
)
from fwrouter_api.services.servers import ensure_routing_global_state
from fwrouter_api.services.traffic import record_traffic_samples
from fwrouter_api.services.ui_state_logs import _summarize_log_event
from fwrouter_api.services import watchdog as watchdog_service
from fwrouter_api.services.watchdog import (
    detect_recent_vpn_traffic_attempts,
    _watchdog_traffic_failure_confirmation,
    _reset_watchdog_traffic_failure_candidate,
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
    _reset_watchdog_traffic_failure_candidate()
    watchdog_service._WATCHDOG_ISSUE_LOGGED_AT_BY_FINGERPRINT.clear()
    watchdog_service._WATCHDOG_LAST_FAILURE_FINGERPRINT = None
    watchdog_service._WATCHDOG_LAST_FAILURE_LOGGED_AT = None
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
    counter_slug = subject_id.replace(":", "_").replace("-", "_")
    record_traffic_samples(
        [
            {
                "counter_key": f"nft:counter:cnt_{counter_slug}_vpn_rx",
                "subject_id": subject_id,
                "path": "vpn",
                "rx_bytes": 100,
                "tx_bytes": 0,
                "metadata": {"source": "nftables"},
            },
            {
                "counter_key": f"nft:counter:cnt_{counter_slug}_vpn_tx",
                "subject_id": subject_id,
                "path": "vpn",
                "rx_bytes": 50,
                "tx_bytes": 0,
                "metadata": {"source": "nftables"},
            },
        ],
        collector="pytest",
        dry_run=False,
    )
    record_traffic_samples(
        [
            {
                "counter_key": f"nft:counter:cnt_{counter_slug}_vpn_rx",
                "subject_id": subject_id,
                "path": "vpn",
                "rx_bytes": 150,
                "tx_bytes": 0,
                "metadata": {"source": "nftables"},
            },
            {
                "counter_key": f"nft:counter:cnt_{counter_slug}_vpn_tx",
                "subject_id": subject_id,
                "path": "vpn",
                "rx_bytes": 80,
                "tx_bytes": 0,
                "metadata": {"source": "nftables"},
            },
        ],
        collector="pytest",
        dry_run=False,
    )


def _configure_confirmed_watchdog_stall(
    monkeypatch,
    *,
    active_server_id: str,
    decision_prefix: str = "decision",
    fake_now: dict[str, datetime] | None = None,
) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": active_server_id},
    )
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": True,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": (fake_now["value"].isoformat() if fake_now else "2026-07-01T00:00:30+00:00"),
            "decision_id": (
                f"{decision_prefix}-{fake_now['value'].isoformat()}" if fake_now else f"{decision_prefix}-static"
            ),
            "total_rx_delta": 0,
            "total_tx_delta": 100,
            "response_observed": False,
            "outbound_observed": True,
            "traffic_stalled": True,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._watchdog_traffic_failure_confirmation",
        lambda **kwargs: {
            "confirmed": True,
            "reason": "stalled_traffic_confirmed",
            "server_id": kwargs.get("active_server_id"),
            "path_key": kwargs.get("path_key"),
        },
    )


def _insert_vpn_counter_snapshot(
    *,
    counter_key: str,
    subject_id: str,
    collected_at: str,
    rx_delta: int,
    tx_delta: int,
    rx_bytes: int | None = None,
    tx_bytes: int | None = None,
    source: str = "nftables",
    metadata_extra: dict[str, object] | None = None,
) -> None:
    metadata = {
        "rx_delta": rx_delta,
        "tx_delta": tx_delta,
        "source": source,
        "activity_observed": rx_delta > 0 or tx_delta > 0,
    }
    metadata.update(metadata_extra or {})
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key,
                subject_id,
                path,
                rx_bytes,
                tx_bytes,
                collected_at,
                metadata_json
            )
            VALUES (?, ?, 'vpn', ?, ?, ?, json(?))
            ON CONFLICT(counter_key) DO UPDATE SET
                subject_id = excluded.subject_id,
                path = excluded.path,
                rx_bytes = excluded.rx_bytes,
                tx_bytes = excluded.tx_bytes,
                collected_at = excluded.collected_at,
                metadata_json = excluded.metadata_json
            """,
            (
                counter_key,
                subject_id,
                rx_bytes if rx_bytes is not None else rx_delta,
                tx_bytes if tx_bytes is not None else tx_delta,
                collected_at,
                json.dumps(metadata),
            ),
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


def test_detect_recent_vpn_traffic_attempts_uses_latest_snapshot_for_failure_signal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-old")
    _seed_subject("lan-current")
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._utc_now",
        lambda: datetime(2026, 7, 1, 0, 1, 5, tzinfo=timezone.utc),
    )

    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_old_vpn_rx",
        subject_id="lan-old",
        collected_at="2026-07-01T00:00:00+00:00",
        rx_delta=250,
        tx_delta=0,
    )
    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_current_vpn_tx",
        subject_id="lan-current",
        collected_at="2026-07-01T00:01:00+00:00",
        rx_delta=0,
        tx_delta=100,
    )

    signal = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert signal["total_rx_delta"] == 250
    assert signal["total_tx_delta"] == 100
    assert signal["latest_rx_delta"] == 0
    assert signal["latest_tx_delta"] == 100
    assert signal["authoritative_rx_delta"] == 0
    assert signal["authoritative_tx_delta"] == 100
    assert signal["traffic_stalled"] is True


def test_detect_recent_vpn_traffic_attempts_ignores_xray_profile_responses(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-stalled")
    collected_at = datetime.now(timezone.utc).isoformat()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode, runtime_state, is_active
            )
            VALUES ('fwrouter:global', 'fwrouter', 'fwrouter:global', 'FWRouter global traffic', 'direct', 'running', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
            )
            VALUES (
                'nft:counter:cnt_lan_stalled_vpn_tx',
                'lan-stalled',
                'vpn',
                0,
                100,
                ?,
                ?
            )
            """,
            (
                collected_at,
                json.dumps({"rx_delta": 0, "tx_delta": 100, "source": "nftables", "activity_observed": True}),
            ),
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode, runtime_state, is_active
            )
            VALUES ('xray:healthy-profile', 'xray', 'xray:healthy-profile', 'Xray profile', 'enabled', 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
            )
            VALUES (
                'xray:subject:xray:healthy-profile',
                'xray:healthy-profile',
                'vpn',
                1000,
                10,
                ?,
                ?
            )
            """,
            (
                collected_at,
                json.dumps({"rx_delta": 1000, "tx_delta": 10, "source": "xray_api", "activity_observed": True}),
            ),
        )

    signal = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert signal["observed"] is True
    assert signal["total_tx_delta"] == 100
    assert signal["total_rx_delta"] == 0
    assert signal["traffic_stalled"] is True
    assert signal["ignored_samples_count"] == 1
    assert signal["ignored_samples"][0]["subject_id"] == "xray:healthy-profile"


def test_detect_recent_vpn_traffic_attempts_uses_explicit_adapter_response_fallback_when_nft_rx_is_absent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-stalled")
    collected_at = datetime.now(timezone.utc).isoformat()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode, runtime_state, is_active
            )
            VALUES ('fwrouter:global', 'fwrouter', 'fwrouter:global', 'FWRouter global traffic', 'direct', 'running', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
            )
            VALUES (
                'nft:counter:cnt_lan_stalled_vpn_tx',
                'lan-stalled',
                'vpn',
                0,
                100,
                ?,
                ?
            )
            """,
            (
                collected_at,
                json.dumps({"rx_delta": 0, "tx_delta": 100, "source": "nftables", "activity_observed": True}),
            ),
        )
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
            )
            VALUES (
                'external-adapter:global',
                'fwrouter:global',
                'vpn',
                1000,
                10,
                ?,
                ?
            )
            """,
            (
                collected_at,
                json.dumps(
                    {
                        "rx_delta": 1000,
                        "tx_delta": 10,
                        "source": "external_adapter",
                        "watchdog_signal": "adapter_response",
                        "connection_type": "external_vpn_module",
                        "activity_observed": True,
                    }
                ),
            ),
        )

    signal = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert signal["observed"] is True
    assert signal["total_rx_delta"] == 1000
    assert signal["total_tx_delta"] == 110
    assert signal["dataplane_rx_delta"] == 0
    assert signal["dataplane_tx_delta"] == 100
    assert signal["adapter_rx_delta"] == 1000
    assert signal["adapter_tx_delta"] == 10
    assert signal["authoritative_rx_delta"] == 1000
    assert signal["authoritative_tx_delta"] == 100
    assert signal["authoritative_response_source"] == "adapter_fallback"
    assert signal["response_observed"] is True
    assert signal["traffic_stalled"] is False


def test_detect_recent_vpn_traffic_attempts_correlates_independent_collectors(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_SIGNAL_CORRELATION_SECONDS", "30")
    get_settings.cache_clear()
    initialize_database()
    _seed_subject("lan-correlated")
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._utc_now",
        lambda: datetime(2026, 7, 1, 0, 0, 20, tzinfo=timezone.utc),
    )

    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_correlated_vpn_tx",
        subject_id="lan-correlated",
        collected_at="2026-07-01T00:00:00+00:00",
        rx_delta=0,
        tx_delta=1000,
    )
    _insert_vpn_counter_snapshot(
        counter_key="external-adapter:correlated",
        subject_id="lan-correlated",
        collected_at="2026-07-01T00:00:08+00:00",
        rx_delta=800,
        tx_delta=0,
        source="external_adapter",
        metadata_extra={
            "watchdog_signal": "adapter_response",
            "connection_type": "external_vpn_module",
        },
    )

    signal = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert signal["path_state"] == "healthy"
    assert signal["authoritative_tx_delta"] == 1000
    assert signal["authoritative_rx_delta"] == 800
    assert signal["authoritative_response_source"] == "adapter_fallback"
    assert signal["traffic_stalled"] is False
    assert signal["current_observation"]["correlation_seconds"] == 30
    assert signal["current_observation"]["tx_observed_at"] == "2026-07-01T00:00:00+00:00"
    assert signal["current_observation"]["rx_observed_at"] == "2026-07-01T00:00:08+00:00"
    assert signal["decision_id"]


def test_detect_recent_vpn_traffic_attempts_does_not_correlate_old_adapter_rx(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_SIGNAL_CORRELATION_SECONDS", "30")
    get_settings.cache_clear()
    initialize_database()
    _seed_subject("lan-old-adapter")
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._utc_now",
        lambda: datetime(2026, 7, 1, 0, 0, 10, tzinfo=timezone.utc),
    )

    _insert_vpn_counter_snapshot(
        counter_key="external-adapter:old",
        subject_id="lan-old-adapter",
        collected_at="2026-06-30T23:58:00+00:00",
        rx_delta=800,
        tx_delta=0,
        source="external_adapter",
        metadata_extra={
            "watchdog_signal": "adapter_response",
            "connection_type": "external_vpn_module",
        },
    )
    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_old_adapter_vpn_tx",
        subject_id="lan-old-adapter",
        collected_at="2026-07-01T00:00:00+00:00",
        rx_delta=0,
        tx_delta=1000,
    )

    signal = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert signal["path_state"] == "suspected_failure"
    assert signal["total_rx_delta"] == 800
    assert signal["authoritative_rx_delta"] == 0
    assert signal["authoritative_tx_delta"] == 1000
    assert signal["authoritative_response_source"] == "none"
    assert signal["traffic_stalled"] is True


def test_detect_recent_vpn_traffic_attempts_decision_id_is_stable_for_same_observation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-decision")
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._utc_now",
        lambda: datetime(2026, 7, 1, 0, 0, 10, tzinfo=timezone.utc),
    )

    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_decision_vpn_tx",
        subject_id="lan-decision",
        collected_at="2026-07-01T00:00:00+00:00",
        rx_delta=0,
        tx_delta=1000,
    )

    first = detect_recent_vpn_traffic_attempts(window_seconds=300)
    second = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert first["decision_id"] == second["decision_id"]
    assert first["path_state"] == "suspected_failure"


def test_detect_recent_vpn_traffic_attempts_ignores_non_vpn_external_adapter_signal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-stalled")
    collected_at = datetime.now(timezone.utc).isoformat()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
            )
            VALUES (
                'external-network-source:global',
                'lan-stalled',
                'vpn',
                1000,
                10,
                ?,
                ?
            )
            """,
            (
                collected_at,
                json.dumps(
                    {
                        "rx_delta": 1000,
                        "tx_delta": 10,
                        "source": "external_adapter",
                        "watchdog_signal": "adapter_response",
                        "connection_type": "external_network_source",
                        "activity_observed": True,
                    }
                ),
            ),
        )

    signal = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert signal["observed"] is False
    assert signal["ignored_samples_count"] == 1
    assert signal["ignored_samples"][0]["counter_key"] == "external-network-source:global"


def test_detect_recent_vpn_traffic_attempts_treats_global_vpn_mark_as_outbound(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-stalled")
    collected_at = datetime.now(timezone.utc).isoformat()

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, stable_key, display_name, desired_mode, runtime_state, is_active
            )
            VALUES ('fwrouter:global', 'fwrouter', 'fwrouter:global', 'FWRouter global traffic', 'direct', 'running', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
            )
            VALUES (
                'nft:counter:cnt_lan_stalled_vpn_tx',
                'lan-stalled',
                'vpn',
                0,
                100,
                ?,
                ?
            )
            """,
            (
                collected_at,
                json.dumps({"rx_delta": 0, "tx_delta": 100, "source": "nftables", "activity_observed": True}),
            ),
        )
        connection.execute(
            """
            INSERT INTO traffic_counter_snapshots (
                counter_key, subject_id, path, rx_bytes, tx_bytes, collected_at, metadata_json
            )
            VALUES (
                'fwrouter:global:vpn',
                'fwrouter:global',
                'vpn',
                1000,
                0,
                ?,
                ?
            )
            """,
            (
                collected_at,
                json.dumps(
                    {
                        "rx_delta": 1000,
                        "tx_delta": 0,
                        "source": "nftables",
                        "scope": "global",
                        "activity_observed": True,
                    }
                ),
            ),
        )

    signal = detect_recent_vpn_traffic_attempts(window_seconds=300)

    assert signal["total_rx_delta"] == 1000
    assert signal["total_tx_delta"] == 100
    assert signal["dataplane_rx_delta"] == 0
    assert signal["dataplane_tx_delta"] == 1100
    assert signal["authoritative_rx_delta"] == 0
    assert signal["authoritative_tx_delta"] == 1100
    assert signal["authoritative_response_source"] == "none"
    assert signal["response_observed"] is False
    assert signal["traffic_stalled"] is True


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
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-healthy"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
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
    assert result["status"] == "healthy_traffic"
    assert result["traffic_signal"]["observed"] is True
    assert result["traffic_signal"]["response_observed"] is True
    assert result["active_target_id"] == "srv-healthy"
    assert result["failover_supported"] is True
    assert result["cooldown_active"] is False
    assert result["active_check"]["ok"] is True
    assert result["active_check"]["last_ping_ms"] == 42
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
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-cached"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("fresh ping must be reused")),
    )

    result = run_vpn_watchdog_auto_check(
        allow_switch=False,
        traffic_window_seconds=300,
    )

    assert result["ok"] is True
    assert result["status"] == "healthy_traffic"
    assert result["active_check"]["cached"] is True
    assert result["active_check"]["last_ping_ms"] == 33


def test_watchdog_auto_check_fails_over_when_healthy_traffic_has_degraded_active_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_ACTIVE_PROBE_MAX_LATENCY_MS", "3000")
    get_settings.cache_clear()
    initialize_database()
    _seed_subject("lan-degraded")
    _set_global_vpn_auto("srv-degraded")
    _record_vpn_activity("lan-degraded")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-degraded"},
    )
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
        lambda **kwargs: {
            "ok": True,
            "server_id": "srv-degraded",
            "status": "success",
            "last_ping_ms": 4500,
            "latency_label": "4500 ms",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": None,
            "error_message": None,
            "updated_state": kwargs.get("update_state", False),
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: {
            "ok": True,
            "applied": False,
            "active_before": "srv-degraded",
            "active_after": "srv-candidate",
            "selected_server_id": "srv-candidate",
            "selected_server_name": "Candidate",
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=False, traffic_window_seconds=300)

    assert result["status"] == "failover_candidate_found"
    assert result["path_state"] == "degraded_active_probe"
    assert result["active_check"]["ok"] is False
    assert result["active_check"]["status"] == "degraded_latency"
    assert result["active_check"]["error_code"] == "WATCHDOG_ACTIVE_LATENCY_DEGRADED"
    assert result["selector"]["selected_server_id"] == "srv-candidate"


def test_watchdog_auto_check_applies_failover_when_healthy_traffic_has_degraded_active_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_ACTIVE_PROBE_MAX_LATENCY_MS", "3000")
    get_settings.cache_clear()
    initialize_database()
    _seed_subject("lan-degraded-apply")
    _set_global_vpn_auto("srv-degraded")
    _record_vpn_activity("lan-degraded-apply")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    selector_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-degraded"},
    )
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
        lambda **kwargs: {
            "ok": False,
            "server_id": "srv-degraded",
            "status": "timeout",
            "last_ping_ms": None,
            "latency_label": "timeout",
            "checked_by": kwargs.get("checked_by"),
            "test_url": "https://example.test/generate_204",
            "timeout_ms": kwargs.get("timeout_ms"),
            "error_code": "WATCHDOG_ACTIVE_TIMEOUT",
            "error_message": "Active server timeout.",
            "updated_state": kwargs.get("update_state", False),
        },
    )

    def fake_select_vpn_auto_server(**kwargs):
        selector_calls.append(kwargs)
        return {
            "ok": True,
            "applied": True,
            "active_before": "srv-degraded",
            "active_after": "srv-candidate",
            "selected_server_id": "srv-candidate",
            "selected_server_name": "Candidate",
        }

    monkeypatch.setattr("fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server", fake_select_vpn_auto_server)

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert result["status"] == "failover_applied"
    assert result["path_state"] == "degraded_active_probe"
    assert result["runtime_failover"]["selected_target_id"] == "srv-candidate"
    assert result["selector"]["selected_server_id"] == "srv-candidate"
    assert result["failover_cooldown"]["active"] is True
    assert selector_calls[0]["apply"] is True
    assert selector_calls[0]["exclude_active"] is True


def test_watchdog_auto_check_marks_module_degraded_on_fail_open(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-fail")
    _set_global_vpn_auto("srv-fail")
    _record_vpn_activity("lan-fail")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-fail"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": True,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": "2026-07-01T00:00:30+00:00",
            "total_rx_delta": 0,
            "total_tx_delta": 100,
            "response_observed": False,
            "outbound_observed": True,
            "traffic_stalled": True,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._watchdog_traffic_failure_confirmation",
        lambda **kwargs: {
            "confirmed": True,
            "reason": "stalled_traffic_confirmed",
            "server_id": kwargs.get("active_server_id"),
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("confirmed traffic stall must not active-probe")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
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
    assert result["active_check"]["source"] == "traffic_counter_snapshots"
    assert module is not None
    assert module["runtime_state"] == "degraded"
    assert module["error_code"] == "WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED"


def test_watchdog_external_vpn_adapter_skips_mihomo_selector(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-external")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    external_adapter = {
        "role": "vpn_dataplane",
        "adapter_id": "external_vpn_module",
        "lifecycle_mode": "external",
        "ready": True,
        "source": {
            "kind": "external",
            "system_id": "external-vpn-sing-box",
            "runtime_type": "sing-box",
        },
        "contour": {"adapter": "external_vpn_module"},
        "reason": "external_vpn_module_ready",
    }
    monkeypatch.setattr("fwrouter_api.services.watchdog.active_vpn_dataplane_adapter", lambda: external_adapter)
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: (_ for _ in ()).throw(AssertionError("external adapter must not read vpn-auto state")),
    )
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": False,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "response_observed": False,
            "outbound_observed": False,
            "traffic_stalled": False,
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert result["ok"] is True
    assert result["status"] == "no_failure_no_traffic"
    assert result["active_server_id"] == "external-vpn-sing-box"
    assert result["vpn_adapter"]["adapter_id"] == "external_vpn_module"


def test_watchdog_external_vpn_adapter_reports_missing_failover_adapter(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-external")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    external_adapter = {
        "role": "vpn_dataplane",
        "adapter_id": "external_vpn_module",
        "lifecycle_mode": "external",
        "ready": True,
        "source": {
            "kind": "external",
            "system_id": "external-vpn-sing-box",
            "runtime_type": "sing-box",
        },
        "contour": {"adapter": "external_vpn_module"},
        "reason": "external_vpn_module_ready",
    }
    monkeypatch.setattr("fwrouter_api.services.watchdog.active_vpn_dataplane_adapter", lambda: external_adapter)
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("external adapter must not switch Mihomo selector")),
    )
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": True,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": "2026-07-01T00:00:30+00:00",
            "total_rx_delta": 0,
            "total_tx_delta": 100,
            "response_observed": False,
            "outbound_observed": True,
            "traffic_stalled": True,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._watchdog_traffic_failure_confirmation",
        lambda **kwargs: {
            "confirmed": True,
            "reason": "stalled_traffic_confirmed",
            "server_id": kwargs.get("active_server_id"),
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)
    module = get_module_state("watchdog")

    assert result["ok"] is False
    assert result["status"] == "external_runtime_failover_unavailable"
    assert result["active_server_id"] == "external-vpn-sing-box"
    assert result["selector"] is None
    assert module is not None
    assert module["runtime_state"] == "degraded"
    assert module["error_code"] == "WATCHDOG_EXTERNAL_FAILOVER_UNAVAILABLE"


def test_watchdog_external_vpn_adapter_uses_selector_api_failover(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-external")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    external_adapter = {
        "role": "vpn_dataplane",
        "adapter_id": "external_vpn_module",
        "lifecycle_mode": "external",
        "ready": True,
        "source": {
            "kind": "external",
            "system_id": "external-vpn-sing-box",
            "runtime_type": "sing-box",
            "capabilities": {"supports_selector_api": True},
            "endpoints": {
                "selector_state_url": "http://127.0.0.1:9191/selector",
                "selector_failover_url": "http://127.0.0.1:9191/failover",
            },
        },
        "contour": {"adapter": "external_vpn_module"},
        "reason": "external_vpn_module_ready",
    }
    posts: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status = 200
            self._payload = payload
            self.headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit: int = -1) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        url = getattr(request, "full_url", request)
        if url == "http://127.0.0.1:9191/selector":
            return FakeResponse({"ok": True, "selection_mode": "auto", "active_target_id": "external-a"})
        if url == "http://127.0.0.1:9191/failover":
            posts.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse({"ok": True, "applied": True, "active_after": "external-b"})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("fwrouter_api.services.watchdog.active_vpn_dataplane_adapter", lambda: external_adapter)
    monkeypatch.setattr("fwrouter_api.services.vpn_runtime_control.urlopen", fake_urlopen)
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("external selector API must not call Mihomo")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": True,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": "2026-07-01T00:00:30+00:00",
            "total_rx_delta": 0,
            "total_tx_delta": 100,
            "response_observed": False,
            "outbound_observed": True,
            "traffic_stalled": True,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._watchdog_traffic_failure_confirmation",
        lambda **kwargs: {
            "confirmed": True,
            "reason": "stalled_traffic_confirmed",
            "server_id": kwargs.get("active_server_id"),
            "path_key": kwargs.get("path_key"),
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert result["ok"] is True
    assert result["status"] == "failover_applied"
    assert result["action"] == "external_vpn_failover"
    assert result["active_server_id"] == "external-a"
    assert result["runtime_failover"]["selected_target_id"] == "external-b"
    assert posts == [
        {
            "apply": True,
            "reason": "watchdog_failover:auto_watchdog_check",
            "requested_by": "fwrouter_watchdog",
            "exclude_target_id": "external-a",
            "candidate_limit": 4,
            "timeout_ms": 10000,
        }
    ]


def test_watchdog_auto_check_waits_for_traffic_failure_confirmation(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-pending")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-pending"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": True,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": "2026-07-01T00:00:00+00:00",
            "total_rx_delta": 0,
            "total_tx_delta": 100,
            "response_observed": False,
            "outbound_observed": True,
            "traffic_stalled": True,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("pending traffic failure must not probe")),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("pending traffic failure must not switch")),
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert result["ok"] is True
    assert result["status"] == "traffic_failure_pending"
    assert result["allow_switch"] is False
    assert result["traffic_failure_confirmation"]["pending"] is True
    assert result["active_check"] is None
    assert result["selector"] is None


def test_watchdog_auto_check_persists_failover_cooldown(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_FAILOVER_COOLDOWN_SECONDS", "30")
    get_settings.cache_clear()
    initialize_database()
    _set_global_vpn_auto("srv-cooldown")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    fake_now = {"value": datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)}
    selector_calls: list[dict[str, object]] = []

    monkeypatch.setattr("fwrouter_api.services.watchdog._utc_now", lambda: fake_now["value"])
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-cooldown"},
    )
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": True,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": fake_now["value"].isoformat(),
            "decision_id": f"decision-{fake_now['value'].second}",
            "total_rx_delta": 0,
            "total_tx_delta": 100,
            "response_observed": False,
            "outbound_observed": True,
            "traffic_stalled": True,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._watchdog_traffic_failure_confirmation",
        lambda **kwargs: {
            "confirmed": True,
            "reason": "stalled_traffic_confirmed",
            "server_id": kwargs.get("active_server_id"),
            "path_key": kwargs.get("path_key"),
        },
    )

    def fake_select_vpn_auto_server(**kwargs):
        selector_calls.append(kwargs)
        return {
            "ok": True,
            "applied": True,
            "active_before": "srv-cooldown",
            "active_after": "srv-next",
            "selected_server_id": "srv-next",
        }

    monkeypatch.setattr("fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server", fake_select_vpn_auto_server)

    first = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)
    watchdog_service._WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = None
    clear_live_probe_cache()
    fake_now["value"] = datetime(2026, 7, 1, 0, 0, 10, tzinfo=timezone.utc)
    second = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert first["status"] == "failover_applied"
    assert first["failover_cooldown"]["cooldown_until"] == "2026-07-01T00:00:30+00:00"
    assert second["status"] == "failover_cooldown"
    assert second["allow_switch"] is False
    assert second["failover_cooldown"]["remaining_seconds"] == 20
    assert second["cooldown_active"] is True
    assert second["cooldown_until"] == "2026-07-01T00:00:30+00:00"
    assert second["cooldown_remaining_seconds"] == 20
    assert second["path_state"] == "confirmed_failure"
    assert len(selector_calls) == 1


def test_watchdog_auto_check_dry_run_does_not_start_failover_cooldown(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_FAILOVER_COOLDOWN_SECONDS", "30")
    get_settings.cache_clear()
    initialize_database()
    _set_global_vpn_auto("srv-dry-run")
    set_module_desired_state("watchdog", "enabled", run_now=False)
    _configure_confirmed_watchdog_stall(monkeypatch, active_server_id="srv-dry-run")

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: {
            "ok": True,
            "applied": False,
            "active_before": "srv-dry-run",
            "active_after": "srv-candidate",
            "selected_server_id": "srv-candidate",
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=False, traffic_window_seconds=300)

    assert result["status"] == "failover_candidate_found"
    assert result["cooldown_active"] is False
    assert result["failover_cooldown"]["active"] is False
    with db_session() as connection:
        row = connection.execute("SELECT cooldown_until FROM watchdog_state WHERE id = 1").fetchone()
    assert row is None or row["cooldown_until"] is None


def test_watchdog_auto_check_failed_failover_does_not_start_cooldown(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_FAILOVER_COOLDOWN_SECONDS", "30")
    get_settings.cache_clear()
    initialize_database()
    _set_global_vpn_auto("srv-failed-cooldown")
    set_module_desired_state("watchdog", "enabled", run_now=False)
    _configure_confirmed_watchdog_stall(monkeypatch, active_server_id="srv-failed-cooldown")

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: {
            "ok": False,
            "applied": False,
            "reason": kwargs.get("reason"),
            "selected_server_id": None,
        },
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert result["status"] == "fail_open_direct_recommended"
    assert result["cooldown_active"] is False
    with db_session() as connection:
        row = connection.execute("SELECT cooldown_until FROM watchdog_state WHERE id = 1").fetchone()
    assert row is None or row["cooldown_until"] is None


def test_watchdog_auto_check_switches_after_cooldown_with_fresh_confirmed_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FWROUTER_WATCHDOG_FAILOVER_COOLDOWN_SECONDS", "30")
    get_settings.cache_clear()
    initialize_database()
    _set_global_vpn_auto("srv-after-cooldown")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    fake_now = {"value": datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr("fwrouter_api.services.watchdog._utc_now", lambda: fake_now["value"])
    _configure_confirmed_watchdog_stall(
        monkeypatch,
        active_server_id="srv-after-cooldown",
        decision_prefix="fresh",
        fake_now=fake_now,
    )
    selector_calls: list[dict[str, object]] = []

    def fake_select_vpn_auto_server(**kwargs):
        selector_calls.append(kwargs)
        return {
            "ok": True,
            "applied": True,
            "active_before": "srv-after-cooldown",
            "active_after": f"srv-next-{len(selector_calls)}",
            "selected_server_id": f"srv-next-{len(selector_calls)}",
        }

    monkeypatch.setattr("fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server", fake_select_vpn_auto_server)

    first = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)
    fake_now["value"] = datetime(2026, 7, 1, 0, 0, 31, tzinfo=timezone.utc)
    second = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert first["status"] == "failover_applied"
    assert second["status"] == "failover_applied"
    assert second["cooldown_until"] == "2026-07-01T00:01:01+00:00"
    assert len(selector_calls) == 2


def test_watchdog_auto_check_monitors_manual_selection_without_failover(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-manual")
    with db_session() as connection:
        connection.execute("UPDATE routing_global_state SET server_mode = 'fixed' WHERE id = 1")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-manual"},
    )
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog.detect_recent_vpn_traffic_attempts",
        lambda **kwargs: {
            "observed": True,
            "authoritative": True,
            "safe_for_watchdog_auto": True,
            "last_collected_at": "2026-07-01T00:00:30+00:00",
            "total_rx_delta": 0,
            "total_tx_delta": 100,
            "response_observed": False,
            "outbound_observed": True,
            "traffic_stalled": True,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.watchdog._watchdog_traffic_failure_confirmation",
        lambda **kwargs: {"confirmed": True, "reason": "stalled_traffic_confirmed"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("manual mode must not switch")),
    )

    result = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert result["status"] == "manual_selection"
    assert result["selection_mode"] == "manual"
    assert result["allow_switch"] is False
    assert result["action"] == "none"
    assert result["module"]["error_code"] == "WATCHDOG_MANUAL_SELECTION"


def test_watchdog_emulated_server_outage_requires_fresh_stalled_traffic_before_failover(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-outage")
    _set_global_vpn_auto("srv-outage")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    fake_now = {"value": datetime(2026, 7, 1, 0, 0, 5, tzinfo=timezone.utc)}
    selector_calls: list[dict[str, object]] = []

    monkeypatch.setattr("fwrouter_api.services.watchdog._utc_now", lambda: fake_now["value"])
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-outage"},
    )
    monkeypatch.setattr("fwrouter_api.services.watchdog._has_scoped_vpn_subjects", lambda: False)
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
        lambda **kwargs: {
            "ok": True,
            "server_id": "srv-outage",
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

    def fake_select_vpn_auto_server(**kwargs):
        selector_calls.append(kwargs)
        return {
            "ok": True,
            "selected_server_id": "srv-recovered",
            "selected_server_name": "Recovered",
            "active_after": "srv-recovered",
        }

    monkeypatch.setattr("fwrouter_api.services.vpn_runtime_control.select_vpn_auto_server", fake_select_vpn_auto_server)

    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_outage_vpn_tx",
        subject_id="lan-outage",
        collected_at="2026-07-01T00:00:00+00:00",
        rx_delta=0,
        tx_delta=120,
    )
    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_outage_vpn_rx",
        subject_id="lan-outage",
        collected_at="2026-07-01T00:00:00+00:00",
        rx_delta=110,
        tx_delta=0,
    )

    healthy = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert healthy["status"] == "healthy_traffic"
    assert healthy["traffic_signal"]["dataplane_rx_delta"] == 110
    assert healthy["traffic_signal"]["dataplane_tx_delta"] == 120
    assert selector_calls == []

    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_outage_vpn_tx",
        subject_id="lan-outage",
        collected_at="2026-07-01T00:01:00+00:00",
        rx_delta=0,
        tx_delta=140,
    )
    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_outage_vpn_rx",
        subject_id="lan-outage",
        collected_at="2026-07-01T00:01:00+00:00",
        rx_delta=0,
        tx_delta=0,
    )
    fake_now["value"] = datetime(2026, 7, 1, 0, 1, 5, tzinfo=timezone.utc)

    first_stall = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert first_stall["status"] == "traffic_failure_pending"
    assert first_stall["traffic_signal"]["traffic_stalled"] is True
    assert first_stall["traffic_failure_confirmation"]["reason"] == "first_stalled_traffic_snapshot"
    assert selector_calls == []

    fake_now["value"] = datetime(2026, 7, 1, 0, 2, 10, tzinfo=timezone.utc)

    same_snapshot = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert same_snapshot["status"] == "traffic_failure_pending"
    assert same_snapshot["traffic_failure_confirmation"]["reason"] == "same_stalled_traffic_snapshot"
    assert selector_calls == []

    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_outage_vpn_tx",
        subject_id="lan-outage",
        collected_at="2026-07-01T00:02:10+00:00",
        rx_delta=0,
        tx_delta=160,
    )
    _insert_vpn_counter_snapshot(
        counter_key="nft:counter:cnt_lan_outage_vpn_rx",
        subject_id="lan-outage",
        collected_at="2026-07-01T00:02:10+00:00",
        rx_delta=0,
        tx_delta=0,
    )
    fake_now["value"] = datetime(2026, 7, 1, 0, 2, 11, tzinfo=timezone.utc)

    confirmed = run_vpn_watchdog_auto_check(allow_switch=True, traffic_window_seconds=300)

    assert confirmed["status"] == "failover_applied"
    assert confirmed["action"] == "switch_vpn_auto"
    assert confirmed["traffic_failure_confirmation"]["reason"] == "stalled_traffic_confirmed"
    assert len(selector_calls) == 1
    assert selector_calls[0]["apply"] is True
    assert selector_calls[0]["reason"] == "watchdog_failover:auto_watchdog_check"
    assert selector_calls[0]["check_on_demand"] is True
    assert selector_calls[0]["exclude_active"] is True
    assert selector_calls[0]["post_check"] is True


def test_watchdog_traffic_failure_confirmation_requires_fresh_snapshot(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    traffic_signal = {
        "last_collected_at": "2026-07-01T00:00:00+00:00",
        "total_rx_delta": 0,
        "total_tx_delta": 100,
        "active_samples_count": 1,
        "traffic_stalled": True,
    }

    first = _watchdog_traffic_failure_confirmation(
        active_server_id="srv-stalled",
        traffic_signal=traffic_signal,
        confirm_seconds=30,
    )
    second = _watchdog_traffic_failure_confirmation(
        active_server_id="srv-stalled",
        traffic_signal=traffic_signal,
        confirm_seconds=30,
    )

    assert first["pending"] is True
    assert first["confirmed"] is False
    assert second["pending"] is True
    assert second["confirmed"] is False
    assert second["reason"] == "same_stalled_traffic_snapshot"


def test_watchdog_traffic_failure_confirmation_persists_candidate_across_restart(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    traffic_signal = {
        "last_collected_at": "2026-07-01T00:00:00+00:00",
        "decision_id": "decision-a",
        "total_rx_delta": 0,
        "total_tx_delta": 100,
        "active_samples_count": 1,
        "traffic_stalled": True,
    }

    first = _watchdog_traffic_failure_confirmation(
        active_server_id="srv-stalled",
        traffic_signal=traffic_signal,
        confirm_seconds=30,
    )

    watchdog_service._WATCHDOG_TRAFFIC_FAILURE_CANDIDATE = None

    second = _watchdog_traffic_failure_confirmation(
        active_server_id="srv-stalled",
        traffic_signal=traffic_signal,
        confirm_seconds=30,
    )

    assert first["reason"] == "first_stalled_traffic_snapshot"
    assert second["pending"] is True
    assert second["confirmed"] is False
    assert second["reason"] == "same_stalled_traffic_snapshot"


def test_watchdog_auto_check_suppresses_switching_without_fresh_signal(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _set_global_vpn_auto("srv-stale")
    set_module_desired_state("watchdog", "enabled", run_now=False)

    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
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
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
        lambda: {"active_auto_server_valid": True, "active_auto_server_id": "srv-logged"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
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
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
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

    assert list_technical_logs(component="watchdog") == []

    summary = _summarize_log_event(
        {
            "timestamp": "2026-07-01T00:00:00+00:00",
            "level": "warning",
            "component": "watchdog",
            "event_type": "watchdog_switch_suppressed",
            "message": "Watchdog did not switch VPN-auto because the traffic signal is stale or unavailable.",
            "details": {
                "status": "paused_signal_unavailable",
                "error_code": "WATCHDOG_SIGNAL_UNAVAILABLE",
                "traffic_signal": {
                    "authoritative": False,
                    "observed": False,
                    "last_collected_at": None,
                },
            },
        },
        technical=True,
    )
    assert summary["message"] == "Watchdog не стал менять VPN-сервер: Нет свежего сигнала трафика"
    assert summary["ui_visible"] is False
    assert summary["details"]["Код"] == "WATCHDOG_SIGNAL_UNAVAILABLE"
    assert summary["details"]["Статус"] == "Нет свежего сигнала трафика"
    assert "Нет свежего достоверного снимка" in summary["details"]["Причина"]


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
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
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
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
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
        "fwrouter_api.services.vpn_runtime_control.get_vpn_auto_state",
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
            "total_rx_delta": 10,
            "total_tx_delta": 5,
            "response_observed": True,
            "outbound_observed": True,
            "traffic_stalled": False,
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.vpn_runtime_control.check_active_server_delay",
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
    assert result["status"] == "healthy_traffic"
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
        "fwrouter_api.services.runtime_convergence.inspect_dnsmasq_selective_status",
        lambda: {"ok": False, "missing": ["nftset_probe_failed"]},
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
    assert result["dnsmasq"]["preflight_action"] == "reconcile_after_status_unhealthy"


def test_runtime_convergence_skips_dnsmasq_reconcile_when_status_is_healthy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                server_mode = 'auto',
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    calls = {"dnsmasq": 0, "dataplane": 0}
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence._compute_has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_dnsmasq_rules",
        lambda: calls.__setitem__("dnsmasq", calls["dnsmasq"] + 1)
        or {"ok": True, "restart_required": True},
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
    assert result["repaired"] is False
    assert result["dnsmasq"]["skipped"] is True
    assert result["dnsmasq"]["preflight_action"] == "skip_reconcile_status_ok"
    assert calls == {"dnsmasq": 0, "dataplane": 1}


def test_runtime_convergence_skips_dnsmasq_reconcile_for_transient_nftset_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                server_mode = 'auto',
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    calls = {"dnsmasq": 0, "dataplane": 0}
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence._compute_has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.inspect_dnsmasq_selective_status",
        lambda: {
            "ok": False,
            "missing": [
                "dnsmasq_nftset_probe_materialization_missing:direct:2ip.ru:188.40.167.82",
            ],
            "nftset_probe_status": {
                "ok": False,
                "status": "unhealthy",
                "restart_recommended": True,
                "missing": [
                    "dnsmasq_nftset_probe_materialization_missing:direct:2ip.ru:188.40.167.82",
                ],
                "probes": [
                    {
                        "action": "DIRECT",
                        "domain": "2ip.ru",
                        "ok": False,
                        "error_code": "dnsmasq_probe_materialization_missing",
                    },
                    {
                        "action": "VPN",
                        "domain": "facebook.com",
                        "ok": True,
                        "error_code": None,
                    },
                ],
            },
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_dnsmasq_rules",
        lambda: calls.__setitem__("dnsmasq", calls["dnsmasq"] + 1)
        or {"ok": True, "restart_required": True},
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
    assert result["repaired"] is False
    assert result["dnsmasq"]["skipped"] is True
    assert result["dnsmasq"]["preflight_action"] == "skip_reconcile_nftset_probe_transient"
    assert calls == {"dnsmasq": 0, "dataplane": 1}


def test_runtime_convergence_skips_dnsmasq_reconcile_for_single_probe_resolve_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                server_mode = 'auto',
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    calls = {"dnsmasq": 0, "dataplane": 0}
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence._compute_has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.inspect_dnsmasq_selective_status",
        lambda: {
            "ok": False,
            "missing": ["dnsmasq_nftset_probe_resolve_failed:direct:2ip.ru"],
            "nftset_probe_status": {
                "ok": False,
                "status": "unhealthy",
                "restart_recommended": True,
                "missing": ["dnsmasq_nftset_probe_resolve_failed:direct:2ip.ru"],
                "probes": [
                    {
                        "action": "DIRECT",
                        "domain": "2ip.ru",
                        "ok": False,
                        "error_code": "dnsmasq_probe_resolve_failed",
                    },
                    {
                        "action": "VPN",
                        "domain": "facebook.com",
                        "ok": True,
                        "error_code": None,
                    },
                ],
            },
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_dnsmasq_rules",
        lambda: calls.__setitem__("dnsmasq", calls["dnsmasq"] + 1)
        or {"ok": True, "restart_required": True},
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
    assert result["repaired"] is False
    assert result["dnsmasq"]["preflight_action"] == "skip_reconcile_nftset_probe_transient"
    assert calls == {"dnsmasq": 0, "dataplane": 1}


def test_runtime_convergence_reconciles_dnsmasq_when_all_probe_resolves_fail(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                server_mode = 'auto',
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    calls = {"dnsmasq": 0, "dataplane": 0}
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence._compute_has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.inspect_dnsmasq_selective_status",
        lambda: {
            "ok": False,
            "missing": [
                "dnsmasq_nftset_probe_resolve_failed:direct:2ip.ru",
                "dnsmasq_nftset_probe_resolve_failed:vpn:facebook.com",
            ],
            "nftset_probe_status": {
                "ok": False,
                "status": "unhealthy",
                "restart_recommended": True,
                "missing": [
                    "dnsmasq_nftset_probe_resolve_failed:direct:2ip.ru",
                    "dnsmasq_nftset_probe_resolve_failed:vpn:facebook.com",
                ],
                "probes": [
                    {"action": "DIRECT", "domain": "2ip.ru", "ok": False},
                    {"action": "VPN", "domain": "facebook.com", "ok": False},
                ],
            },
        },
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_dnsmasq_rules",
        lambda: calls.__setitem__("dnsmasq", calls["dnsmasq"] + 1)
        or {"ok": True, "restart_required": True, "restart_reason": "nftset_probe_unhealthy"},
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
    assert result["repaired"] is True
    assert result["dnsmasq"]["preflight_action"] == "reconcile_after_status_unhealthy"
    assert calls == {"dnsmasq": 1, "dataplane": 1}


def test_runtime_convergence_enters_cooldown_after_repeated_failures(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_FAILURE_LIMIT", "3")
    monkeypatch.setenv("FWROUTER_RUNTIME_CONVERGENCE_COOLDOWN_SECONDS", "600")
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                server_mode = 'auto',
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    calls = {"dnsmasq": 0, "dataplane": 0}
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence._compute_has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.inspect_dnsmasq_selective_status",
        lambda: {"ok": True, "missing": []},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_dnsmasq_rules",
        lambda: calls.__setitem__("dnsmasq", calls["dnsmasq"] + 1)
        or {"ok": True, "restart_required": True},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_current_routing_if_drift",
        lambda **kwargs: calls.__setitem__("dataplane", calls["dataplane"] + 1)
        or {
            "ok": False,
            "action": "reapply_global_mode",
            "error_code": "SELECTIVE_ENFORCEMENT_NOT_READY",
            "error_message": "owned table missing",
        },
    )

    for _ in range(3):
        result = run_runtime_convergence_check(
            requested_by="pytest",
            log_events=False,
            force=True,
        )

    assert result["ok"] is False
    assert result["cooldown_failure_count"] == 3
    assert result["cooldown_until"] is not None
    assert calls == {"dnsmasq": 0, "dataplane": 3}

    cooldown = run_runtime_convergence_check(
        requested_by="pytest-scheduler",
        log_events=False,
        force=False,
    )

    assert cooldown["ok"] is False
    assert cooldown["status"] == "cooldown"
    assert cooldown["checked"] is False
    assert cooldown["suppressed"] is True
    assert calls == {"dnsmasq": 0, "dataplane": 3}


def test_runtime_convergence_skips_dnsmasq_when_dataplane_repair_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    ensure_routing_global_state()
    with db_session() as connection:
        connection.execute(
            """
            UPDATE routing_global_state
            SET
                desired_mode = 'selective',
                applied_mode = 'selective',
                server_mode = 'auto',
                apply_state = 'clean',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """
        )

    calls = {"dnsmasq_inspect": 0, "dnsmasq_reconcile": 0, "dataplane": 0}
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence._compute_has_scoped_vpn_subjects",
        lambda: False,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.inspect_dnsmasq_selective_status",
        lambda: calls.__setitem__("dnsmasq_inspect", calls["dnsmasq_inspect"] + 1)
        or {"ok": False, "missing": ["nftset_probe_failed"]},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_dnsmasq_rules",
        lambda: calls.__setitem__("dnsmasq_reconcile", calls["dnsmasq_reconcile"] + 1)
        or {"ok": True, "restart_required": True},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.runtime_convergence.reconcile_current_routing_if_drift",
        lambda **kwargs: calls.__setitem__("dataplane", calls["dataplane"] + 1)
        or {
            "ok": False,
            "action": "reapply_global_mode",
            "error_code": "SELECTIVE_ENFORCEMENT_NOT_READY",
            "error_message": "owned table missing",
        },
    )

    result = run_runtime_convergence_check(
        requested_by="pytest",
        log_events=False,
        force=True,
    )

    assert result["ok"] is False
    assert result["error_code"] == "SELECTIVE_ENFORCEMENT_NOT_READY"
    assert result["dnsmasq"]["skipped"] is True
    assert result["dnsmasq"]["preflight_action"] == "skip_after_dataplane_repair_failed"
    assert calls == {"dnsmasq_inspect": 0, "dnsmasq_reconcile": 0, "dataplane": 1}


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
