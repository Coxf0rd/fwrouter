from fwrouter_api.services import bootstrap, logs, ui_state_logs
from fwrouter_api.services.logs import list_technical_logs
from fwrouter_api.services.ui_state_logs import (
    _localized_log_details,
    _summarize_log_event,
    _watchdog_message_for_event,
    summarize_ui_log_events,
)
from fwrouter_api.services.watchdog_decision_logs import write_watchdog_decision_log
from fwrouter_api.services.ui_text import SUPPORTED_UI_TEXT_LOCALES, _ui_text_reason, _ui_text_title


def test_ui_text_registry_defaults_to_russian() -> None:
    assert _ui_text_title("traffic.metric", "vpn_tx_bytes") == "VPN выход"
    assert (
        _ui_text_reason("watchdog.status", "runtime_unavailable")
        == "Активный VPN runtime не готов или не отвечает, поэтому сервер не менялся."
    )


def test_ui_text_registry_supports_english_locale() -> None:
    assert _ui_text_title("traffic.metric", "vpn_tx_bytes", locale="en") == "VPN outbound"
    assert (
        _ui_text_reason("watchdog.status", "runtime_unavailable", locale="en-US")
        == "The active VPN runtime is not ready or not responding, so the server was not changed."
    )
    assert (
        _watchdog_message_for_event(
            "watchdog_switch_applied",
            {"status": "failover_applied"},
            locale="en-US",
        )
        == "Watchdog changed the VPN server"
    )


def test_watchdog_log_summary_supports_english_locale() -> None:
    event = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "warning",
        "component": "watchdog",
        "event_type": "watchdog_switch_suppressed",
        "message": "Raw diagnostic.",
        "details": {
            "status": "active_quality_degraded_pending",
            "error_code": "WATCHDOG_ACTIVE_QUALITY_DEGRADED_PENDING",
            "action": "none",
            "allow_switch": False,
            "active_server_id": "srv-active",
            "traffic_signal": {
                "observed": True,
                "response_observed": True,
                "last_collected_at": "2026-07-01T00:00:00+00:00",
            },
            "active_quality_confirmation": {
                "bad_checks": 2,
                "bad_checks_required": 2,
                "age_seconds": 68,
                "confirm_seconds": 180,
            },
        },
    }

    summary = _summarize_log_event(event, technical=True, locale="en-US")

    assert summary["message"] == "Watchdog did not change the VPN server"
    assert summary["details"]["Status"] == "Server quality is degraded, confirmation is in progress"
    assert "confirmation window" in summary["details"]["Reason"]
    assert summary["details"]["Action taken"] == "Server was not changed"
    assert summary["details"]["Switch allowed"] == "No"
    assert summary["details"]["VPN traffic seen"] == "Yes"
    assert summary["details"]["Response traffic"] == "Yes"
    assert summary["details"]["Quality check"] == "2/2"
    assert summary["details"]["Confirmation window"] == "68/180s"
    assert summary["details"]["Code"] == "WATCHDOG_ACTIVE_QUALITY_DEGRADED_PENDING"


def test_legacy_vpn_watchdog_event_summary_is_localized() -> None:
    event = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "warning",
        "component": "watchdog",
        "event_type": "vpn_watchdog_failover",
        "message": "VPN-auto active check failed; failover candidate was applied.",
        "details": {
            "status": "failover_applied",
            "action": "switch_vpn_auto",
            "allow_switch": True,
            "selector": {"active_after": "srv-next"},
        },
    }

    ru_summary = _summarize_log_event(event, locale="ru")
    en_summary = _summarize_log_event(event, locale="en-US")

    assert ru_summary["message"] == "Watchdog сменил VPN-сервер"
    assert ru_summary["details"]["Статус"] == "Failover применен"
    assert "Код статуса" not in ru_summary["details"]
    assert en_summary["message"] == "Watchdog changed the VPN server"
    assert en_summary["details"]["Status"] == "Failover applied"
    assert "Status code" not in en_summary["details"]


def test_legacy_vpn_watchdog_idle_and_healthy_statuses_are_localized() -> None:
    idle = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "info",
        "component": "watchdog",
        "event_type": "vpn_watchdog_no_traffic",
        "message": "No VPN-auto traffic attempts observed.",
        "details": {"status": "no_failure_no_traffic", "action": "none", "allow_switch": False},
    }
    healthy = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "info",
        "component": "watchdog",
        "event_type": "vpn_watchdog_healthy",
        "message": "VPN-auto traffic attempts observed and active server check succeeded.",
        "details": {"status": "healthy", "action": "none", "allow_switch": True},
    }

    idle_summary = _summarize_log_event(idle, locale="ru")
    healthy_summary = _summarize_log_event(healthy, locale="en")

    assert idle_summary["message"] == "Watchdog не стал менять VPN-сервер"
    assert idle_summary["details"]["Статус"] == "Трафика нет, это не считается сбоем"
    assert healthy_summary["message"] == "Watchdog checked the VPN server"
    assert healthy_summary["details"]["Status"] == "VPN server is healthy"


def test_non_watchdog_log_summary_supports_english_locale() -> None:
    event = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "info",
        "component": "bootstrap",
        "event_type": "startup_mihomo_selector_restored",
        "message": "Raw diagnostic.",
        "details": {
            "active_auto_server_id": "srv-active",
            "restored": True,
        },
    }

    summary = _summarize_log_event(event, technical=True, locale="en-US")

    assert summary["message"] == "Selected VPN server restored in runtime"
    assert summary["details"]["Active server"] == "srv-active"
    assert summary["details"]["Restored"] == "Yes"


def test_xray_warning_log_summary_is_localized() -> None:
    event = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "warning",
        "component": "xray",
        "event_type": "xray_binding_materialization_failed",
        "message": "Failed to prepare Mihomo handoff for Xray bindings.",
        "details": {
            "requested_by": "api",
            "message": "Failed to prepare Mihomo handoff for Xray bindings.",
        },
    }

    ru_summary = _summarize_log_event(event, locale="ru")
    en_summary = _summarize_log_event(event, locale="en-US")

    assert ru_summary["ui_visible"] is True
    assert ru_summary["message"] == "Не удалось подготовить Xray runtime bindings"
    assert ru_summary["details"]["Причина"] == "Не удалось подготовить runtime bindings между Xray и маршрутизацией FWRouter."
    assert en_summary["message"] == "Failed to prepare Xray runtime bindings"
    assert en_summary["details"]["Reason"] == "Runtime bindings between Xray and FWRouter routing could not be prepared."
    assert "Причина" not in en_summary["details"]


def test_xray_technical_log_summary_is_localized() -> None:
    event = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "warning",
        "component": "xray",
        "event_type": "xray_service_error",
        "message": "Xray adapter failed.",
        "details": {
            "requested_by": "api",
            "message": "Xray adapter failed.",
        },
    }

    summary = _summarize_log_event(event, technical=True, locale="en")

    assert summary["ui_visible"] is True
    assert summary["message"] == "Xray service error"
    assert summary["details"]["Reason"] == "The Xray service call failed in the adapter."


def test_mihomo_technical_warning_summary_is_localized() -> None:
    event = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "warning",
        "component": "mihomo",
        "event_type": "mihomo_candidate_config_validated",
        "message": "Mihomo candidate config validation failed.",
        "details": {
            "error_code": "MIHOMO_CONFIG_VALIDATION_FAILED",
            "message": "Mihomo candidate config validation failed.",
        },
    }

    ru_summary = _summarize_log_event(event, technical=True, locale="ru")
    en_summary = _summarize_log_event(event, technical=True, locale="en")

    assert ru_summary["message"] == "Candidate-конфигурация Mihomo проверена"
    assert ru_summary["details"]["Причина"].startswith("Backend проверил candidate-конфигурацию Mihomo")
    assert en_summary["message"] == "Mihomo candidate config validated"
    assert en_summary["details"]["Reason"].startswith("The backend checked the Mihomo candidate config")


def test_successful_internal_mihomo_candidate_steps_are_visible_in_ui_journal() -> None:
    written = _summarize_log_event(
        {
            "timestamp": "2026-07-01T00:00:00+00:00",
            "level": "info",
            "component": "mihomo",
            "event_type": "mihomo_candidate_config_written",
            "message": "Mihomo candidate config generated.",
            "details": {"candidate_path": "/var/lib/fwrouter-v2/generated/mihomo/config.next.yaml"},
        },
        technical=True,
        locale="en",
    )
    validated = _summarize_log_event(
        {
            "timestamp": "2026-07-01T00:00:01+00:00",
            "level": "info",
            "component": "mihomo",
            "event_type": "mihomo_candidate_config_validated",
            "message": "Mihomo candidate config validation completed.",
            "details": {"ok": True},
        },
        technical=True,
        locale="en",
    )

    assert written["ui_visible"] is True
    assert validated["ui_visible"] is True


def test_mihomo_candidate_validation_errors_remain_visible() -> None:
    summary = _summarize_log_event(
        {
            "timestamp": "2026-07-01T00:00:00+00:00",
            "level": "warning",
            "component": "mihomo",
            "event_type": "mihomo_candidate_config_validated",
            "message": "Mihomo candidate config validation failed.",
            "details": {
                "ok": False,
                "error_code": "MIHOMO_VPN_AUTO_MISSING",
                "message": "Mihomo candidate config validation failed.",
            },
        },
        technical=True,
        locale="en",
    )

    assert summary["ui_visible"] is True
    assert summary["level"] == "warning"
    assert summary["details"]["Code"] == "MIHOMO_VPN_AUTO_MISSING"


def test_startup_recovery_failures_are_visible_in_ui_journal(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.servers.get_routing_global_state",
        lambda: {"apply_state": "clean", "error_code": None},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_status.build_runtime_enforcement_state",
        lambda: {"active_mode_matches_intent": True, "missing_runtime_requirements": []},
    )

    events = [
        {
            "event_id": "event-1",
            "created_at": "2026-08-29 06:28:46",
            "level": "error",
            "event_type": "mutation_set_global_mode_failed",
            "subject_id": None,
            "message": "Selective enforcement is not ready.",
            "details": {
                "requested_by": "startup-intended-recovery",
                "code": "SELECTIVE_ENFORCEMENT_NOT_READY",
            },
        },
        {
            "event_id": "event-2",
            "created_at": "2026-08-29 06:30:00",
            "level": "error",
            "event_type": "mutation_set_global_mode_failed",
            "subject_id": None,
            "message": "Manual apply failed.",
            "details": {"requested_by": "api", "code": "SELECTIVE_ENFORCEMENT_NOT_READY"},
        },
    ]

    summarized = summarize_ui_log_events(events, locale="en")

    assert summarized[0]["ui_visible"] is True
    assert "resolved_transient" not in summarized[0]
    assert summarized[1]["ui_visible"] is True
    assert "resolved_transient" not in summarized[1]


def test_runtime_scheduler_transients_are_visible_in_ui_journal(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.servers.get_routing_global_state",
        lambda: {"apply_state": "clean", "error_code": None},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_status.build_runtime_enforcement_state",
        lambda: {"active_mode_matches_intent": True, "missing_runtime_requirements": []},
    )

    events = [
        {
            "event_id": "event-1",
            "created_at": "2026-08-29 06:10:10",
            "level": "warning",
            "event_type": "routing_live_drift_detected",
            "subject_id": None,
            "message": "Persisted global routing state does not match live dataplane mode.",
            "details": {
                "requested_by": "runtime_convergence_scheduler",
                "code": "ACTIVE_DATAPLANE_MODE_MISMATCH",
            },
        },
        {
            "event_id": "event-2",
            "created_at": "2026-08-29 06:10:11",
            "level": "warning",
            "event_type": "runtime_convergence_cooldown_entered",
            "subject_id": None,
            "message": "Runtime convergence repair entered cooldown after repeated failures.",
            "details": {"error_code": "SELECTIVE_ENFORCEMENT_NOT_READY"},
        },
        {
            "event_id": "event-3",
            "created_at": "2026-08-29 06:30:00",
            "level": "warning",
            "event_type": "routing_live_drift_detected",
            "subject_id": None,
            "message": "Manual drift check failed.",
            "details": {
                "requested_by": "api",
                "code": "ACTIVE_DATAPLANE_MODE_MISMATCH",
            },
        },
    ]

    summarized = summarize_ui_log_events(events, locale="en")

    assert summarized[0]["ui_visible"] is True
    assert "resolved_transient" not in summarized[0]
    assert summarized[1]["ui_visible"] is True
    assert "resolved_transient" not in summarized[1]
    assert summarized[2]["ui_visible"] is True
    assert "resolved_transient" not in summarized[2]


def test_correlated_apply_failure_is_visible_with_startup_recovery_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.servers.get_routing_global_state",
        lambda: {"apply_state": "clean", "error_code": None},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_status.build_runtime_enforcement_state",
        lambda: {"active_mode_matches_intent": True, "missing_runtime_requirements": []},
    )

    events = [
        {
            "event_id": "event-1",
            "created_at": "2026-08-29 06:08:01",
            "level": "warning",
            "event_type": "apply_failed",
            "subject_id": None,
            "message": "Apply pipeline failed.",
            "details": {
                "job_id": "job-startup",
                "apply_id": "apply-startup",
                "code": "ACTIVE_DATAPLANE_MODE_MISMATCH",
            },
        },
        {
            "event_id": "event-2",
            "created_at": "2026-08-29 06:08:01",
            "level": "error",
            "event_type": "mutation_set_subject_admin_mode_failed",
            "subject_id": None,
            "message": "Active nftables classify chain does not match requested mode selective.",
            "details": {
                "requested_by": "startup-scoped-subject-recovery",
                "job_id": "job-startup",
                "apply_id": "apply-startup",
                "code": "ACTIVE_DATAPLANE_MODE_MISMATCH",
            },
        },
        {
            "event_id": "event-3",
            "created_at": "2026-08-29 06:30:00",
            "level": "warning",
            "event_type": "apply_failed",
            "subject_id": None,
            "message": "Manual apply failed.",
            "details": {
                "job_id": "job-api",
                "apply_id": "apply-api",
                "code": "ACTIVE_DATAPLANE_MODE_MISMATCH",
            },
        },
    ]

    summarized = summarize_ui_log_events(events, locale="en")

    assert summarized[0]["ui_visible"] is True
    assert "resolved_transient" not in summarized[0]
    assert summarized[1]["ui_visible"] is True
    assert "resolved_transient" not in summarized[1]
    assert summarized[2]["ui_visible"] is True
    assert "resolved_transient" not in summarized[2]


def test_unresolved_startup_recovery_failures_remain_visible(monkeypatch) -> None:
    monkeypatch.setattr(
        "fwrouter_api.services.servers.get_routing_global_state",
        lambda: {"apply_state": "failed", "error_code": "SELECTIVE_ENFORCEMENT_NOT_READY"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.dataplane_status.build_runtime_enforcement_state",
        lambda: {
            "active_mode_matches_intent": False,
            "missing_runtime_requirements": ["live_owned_table_missing"],
        },
    )

    summarized = summarize_ui_log_events(
        [
            {
                "event_id": "event-1",
                "created_at": "2026-08-29 06:28:46",
                "level": "error",
                "event_type": "mutation_set_global_mode_failed",
                "subject_id": None,
                "message": "Selective enforcement is not ready.",
                "details": {
                    "requested_by": "startup-intended-recovery",
                    "code": "SELECTIVE_ENFORCEMENT_NOT_READY",
                },
            }
        ],
        locale="en",
    )

    assert summarized[0]["ui_visible"] is True
    assert "resolved_transient" not in summarized[0]


def test_watchdog_noop_suppression_is_visible_in_ui_journal() -> None:
    summary = _summarize_log_event(
        {
            "timestamp": "2026-07-01T00:00:00+00:00",
            "level": "info",
            "component": "watchdog",
            "event_type": "watchdog_switch_suppressed",
            "message": "Watchdog saw outbound-only VPN traffic but is waiting for confirmation before switching.",
            "details": {
                "status": "traffic_failure_pending",
                "action": "none",
                "allow_switch": False,
                "error_code": "WATCHDOG_TRAFFIC_FAILURE_PENDING",
            },
        },
        technical=True,
        locale="en",
    )

    assert summary["level"] == "info"
    assert summary["ui_visible"] is True


def test_real_watchdog_problem_remains_visible() -> None:
    summary = _summarize_log_event(
        {
            "timestamp": "2026-07-01T00:00:00+00:00",
            "level": "warning",
            "component": "watchdog",
            "event_type": "watchdog_switch_suppressed",
            "message": "Watchdog confirmed a VPN traffic stall but found no working failover candidate.",
            "details": {
                "status": "no_working_candidates",
                "action": "none",
                "allow_switch": False,
                "error_code": "WATCHDOG_FAIL_OPEN_DIRECT_RECOMMENDED",
            },
        },
        technical=True,
        locale="en",
    )

    assert summary["ui_visible"] is True
    assert summary["level"] == "warning"


def test_watchdog_noop_writer_downgrades_warning_to_info() -> None:
    write_watchdog_decision_log(
        level="warning",
        event_type="watchdog_switch_suppressed",
        message="Watchdog saw outbound-only VPN traffic but is waiting for confirmation before switching.",
        result={
            "status": "traffic_failure_pending",
            "reason": "auto_watchdog_check",
            "message": "Outbound-only VPN traffic was observed once; failover is pending confirmation.",
            "action": "none",
            "allow_switch": False,
        },
        timestamp="2026-07-01T00:00:00+00:00",
        should_write=lambda _fingerprint: True,
        error_code="WATCHDOG_TRAFFIC_FAILURE_PENDING",
    )

    event = list_technical_logs(component="watchdog", event_type="watchdog_switch_suppressed", limit=1)[0]
    assert event["level"] == "info"


def test_startup_selector_recovery_visible_only_for_real_state_change(monkeypatch) -> None:
    emitted: list[dict] = []
    monkeypatch.setattr(bootstrap, "write_technical_log", lambda **kwargs: emitted.append(kwargs) or kwargs)

    no_change = {
        "ok": True,
        "server_mode": "auto",
        "active_auto_server_id": "srv-a",
        "requested_vpn_global_target": "vpn-auto",
        "vpn_auto_restore": {"ok": True, "skipped": True},
        "vpn_global_restore": {
            "ok": True,
            "details": {
                "selector_before": "vpn-auto",
                "selector_after": "vpn-auto",
                "requested_server_id": "vpn-auto",
            },
        },
    }
    changed = {
        **no_change,
        "vpn_global_restore": {
            "ok": True,
            "details": {
                "selector_before": "DIRECT",
                "selector_after": "vpn-auto",
                "requested_server_id": "vpn-auto",
            },
        },
    }

    monkeypatch.setattr("fwrouter_api.services.selector.restore_mihomo_selector_state", lambda requested_by: no_change)
    result = bootstrap.recover_startup_mihomo_selector()
    assert result["changed"] is False
    assert emitted == []

    monkeypatch.setattr("fwrouter_api.services.selector.restore_mihomo_selector_state", lambda requested_by: changed)
    result = bootstrap.recover_startup_mihomo_selector()
    assert result["changed"] is True
    assert emitted[-1]["event_type"] == "startup_mihomo_selector_restored"


def test_repeated_startup_recovery_is_deduplicated(monkeypatch) -> None:
    logs._LOG_DEDUPE_STATE.clear()
    changed = {
        "ok": True,
        "server_mode": "auto",
        "active_auto_server_id": "srv-a",
        "requested_vpn_global_target": "vpn-auto",
        "vpn_auto_restore": {"ok": True, "skipped": True},
        "vpn_global_restore": {
            "ok": True,
            "details": {
                "selector_before": "DIRECT",
                "selector_after": "vpn-auto",
                "requested_server_id": "vpn-auto",
            },
        },
    }
    monkeypatch.setattr("fwrouter_api.services.selector.restore_mihomo_selector_state", lambda requested_by: changed)

    bootstrap.recover_startup_mihomo_selector()
    bootstrap.recover_startup_mihomo_selector()

    events = list_technical_logs(component="bootstrap", event_type="startup_mihomo_selector_restored")
    assert len(events) == 1


def test_generic_log_detail_truncation_uses_requested_locale() -> None:
    details = {
        "nested": {
            "a": 1,
            "b": 2,
            "c": 3,
            "d": 4,
            "e": 5,
            "f": 6,
        },
        "items": [1, 2, 3, 4, 5, 6],
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
    }

    localized = _localized_log_details(details, locale="en")

    assert localized["nested"]["_truncated"] == "Hidden fields: 1"
    assert localized["items_truncated"] == "Hidden items: 1"
    assert localized["Hidden fields"] == "Hidden fields: 1"


def test_log_formatter_can_use_added_locale_maps() -> None:
    SUPPORTED_UI_TEXT_LOCALES.add("es")
    ui_state_logs.UI_LOG_DETAIL_LABELS_I18N["switch_allowed"]["es"] = "Cambio permitido"
    ui_state_logs.UI_LOG_DETAIL_LABELS_I18N["hidden_fields"]["es"] = "Campos ocultos"
    ui_state_logs.BOOLEAN_LABELS[True]["es"] = "Si"
    ui_state_logs.COUNT_LABEL_FORMATS["clients"]["es"] = "{count} clientes"
    ui_state_logs.HIDDEN_COUNT_FORMATS["fields"]["es"] = "Campos ocultos: {count}"
    ui_state_logs.GENERIC_EVENT_TITLES["es"] = "Evento"

    try:
        event = {
            "timestamp": "2026-07-01T00:00:00+00:00",
            "level": "warning",
            "component": "watchdog",
            "event_type": "watchdog_switch_suppressed",
            "message": "Raw diagnostic.",
            "details": {
                "status": "new_backend_status",
                "allow_switch": True,
            },
        }

        summary = _summarize_log_event(event, technical=True, locale="es")
        localized = _localized_log_details({str(index): index for index in range(9)}, locale="es")
        affected = ui_state_logs._count_label(["a", "b"], "clients", locale="es")
        generic = _summarize_log_event({"details": {}}, locale="es")
    finally:
        SUPPORTED_UI_TEXT_LOCALES.discard("es")
        ui_state_logs.UI_LOG_DETAIL_LABELS_I18N["switch_allowed"].pop("es", None)
        ui_state_logs.UI_LOG_DETAIL_LABELS_I18N["hidden_fields"].pop("es", None)
        ui_state_logs.BOOLEAN_LABELS[True].pop("es", None)
        ui_state_logs.COUNT_LABEL_FORMATS["clients"].pop("es", None)
        ui_state_logs.HIDDEN_COUNT_FORMATS["fields"].pop("es", None)
        ui_state_logs.GENERIC_EVENT_TITLES.pop("es", None)

    assert summary["details"]["Cambio permitido"] == "Si"
    assert localized["Campos ocultos"] == "Campos ocultos: 1"
    assert affected == "2 clientes"
    assert generic["message"] == "Evento"


def test_unknown_log_message_is_compact_and_keeps_diagnostic() -> None:
    raw_message = (
        "This backend diagnostic message is intentionally long because it includes implementation "
        "details that should not dominate the event list column."
    )
    event = {
        "event_id": "event-1",
        "created_at": "2026-07-01T00:00:00+00:00",
        "level": "warning",
        "event_type": "unknown_backend_event",
        "message": raw_message,
        "details": {"message": raw_message},
    }

    summary = _summarize_log_event(event, locale="en")

    assert len(summary["message"]) <= 99
    assert summary["message"].endswith("...")
    assert summary["diagnostic_message"] == raw_message
    assert summary["details"]["Reason"] == raw_message[:240]


def test_known_short_log_message_does_not_duplicate_diagnostic() -> None:
    event = {
        "timestamp": "2026-07-01T00:00:00+00:00",
        "level": "info",
        "component": "bootstrap",
        "event_type": "startup_mihomo_selector_restored",
        "message": "Raw diagnostic.",
        "details": {
            "active_auto_server_id": "srv-active",
        },
    }

    summary = _summarize_log_event(event, technical=True, locale="en")

    assert summary["message"] == "Selected VPN server restored in runtime"
    assert "diagnostic_message" not in summary


def test_ui_text_registry_localized_unknown_fallback() -> None:
    assert _ui_text_title("server.virtual", "unknown", locale="en") == "Virtual server"
    assert (
        _ui_text_reason("error.code", "UNKNOWN_BACKEND_ERROR", locale="en")
        == "Error without a localized explanation; the code is kept in details for diagnostics."
    )
