from __future__ import annotations

from fwrouter_api.services.events import (
    list_recent_events,
    write_diagnostic_event,
    write_operational_event,
)


def test_diagnostic_event_does_not_enter_operational_journal() -> None:
    write_diagnostic_event(
        component="watchdog",
        event_type="probe_result",
        message="Raw watchdog probe.",
        details={"entity_id": "vpn"},
    )

    recent = list_recent_events()

    assert recent["operational"] == []
    assert recent["diagnostic"][0]["event_type"] == "probe_result"


def test_noisy_legacy_watchdog_heartbeat_is_diagnostic_not_user_journal() -> None:
    write_operational_event(
        severity="info",
        event_type="vpn_watchdog_healthy",
        message="Watchdog heartbeat.",
        entity_type="vpn",
        entity_id="vpn",
    )

    recent = list_recent_events()

    assert recent["operational"] == []
    assert recent["diagnostic"][0]["event_type"] == "vpn_watchdog_healthy"


def test_successful_materialize_details_are_diagnostic() -> None:
    write_operational_event(
        severity="info",
        event_type="xray_binding_materialized",
        message="Bindings materialized.",
        entity_type="xray",
        entity_id="xray",
    )

    recent = list_recent_events()

    assert recent["operational"] == []
    assert recent["diagnostic"][0]["event_type"] == "xray_binding_materialized"
