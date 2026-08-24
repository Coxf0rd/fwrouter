from fwrouter_api.services import ui_state_logs
from fwrouter_api.services.ui_state_logs import _localized_log_details, _summarize_log_event, _watchdog_message_for_event
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
        == "Watchdog changed the VPN server: Failover applied"
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

    assert summary["message"] == (
        "Watchdog did not change the VPN server: "
        "Server quality is degraded, confirmation is in progress"
    )
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

    assert ru_summary["message"] == "Watchdog сменил VPN-сервер: Failover применен"
    assert ru_summary["details"]["Статус"] == "Failover применен"
    assert "Код статуса" not in ru_summary["details"]
    assert en_summary["message"] == "Watchdog changed the VPN server: Failover applied"
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

    assert idle_summary["message"] == "Watchdog не стал менять VPN-сервер: Трафика нет, это не считается сбоем"
    assert idle_summary["details"]["Статус"] == "Трафика нет, это не считается сбоем"
    assert healthy_summary["message"] == "Watchdog checked the VPN server: VPN server is healthy"
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


def test_ui_text_registry_localized_unknown_fallback() -> None:
    assert _ui_text_title("server.virtual", "unknown", locale="en") == "Virtual server"
    assert (
        _ui_text_reason("error.code", "UNKNOWN_BACKEND_ERROR", locale="en")
        == "Error without a localized explanation; the code is kept in details for diagnostics."
    )
