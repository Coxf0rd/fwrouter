from fwrouter_api.services.ui_state_logs import _summarize_log_event, _watchdog_message_for_event
from fwrouter_api.services.ui_text import _ui_text_reason, _ui_text_title


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


def test_ui_text_registry_localized_unknown_fallback() -> None:
    assert _ui_text_title("server.virtual", "unknown", locale="en") == "Virtual server"
    assert (
        _ui_text_reason("error.code", "UNKNOWN_BACKEND_ERROR", locale="en")
        == "Error without a localized explanation; the code is kept in details for diagnostics."
    )
