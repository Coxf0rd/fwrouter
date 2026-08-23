from fwrouter_api.services.ui_state_logs import _ui_text_reason, _ui_text_title, _watchdog_message_for_event


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
        == "Watchdog changed the VPN server: VPN server changed by watchdog"
    )


def test_ui_text_registry_localized_unknown_fallback() -> None:
    assert _ui_text_title("server.virtual", "unknown", locale="en") == "Virtual server"
    assert (
        _ui_text_reason("error.code", "UNKNOWN_BACKEND_ERROR", locale="en")
        == "Error without a localized explanation; the code is kept in details for diagnostics."
    )
