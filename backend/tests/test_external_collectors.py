from __future__ import annotations

from fwrouter_api.services import external_collectors


def _connection(*, refresh_mode: str = "interval") -> dict[str, object]:
    return {
        "connection_id": "tailscale-test",
        "system_id": "tailscale-test",
        "enabled": True,
        "integration_mode": "command_probe",
        "refresh_mode": refresh_mode,
        "identity": {"collector": "tailscale-test"},
        "collector_config": {"script_id": "tailscale_status", "timeout_seconds": 5},
    }


def test_successful_non_dry_interval_collector_records_success_only(monkeypatch) -> None:
    marked: list[tuple[str, dict[str, object] | None]] = []
    monkeypatch.setattr(external_collectors, "get_external_connection", lambda _connection_id: _connection())
    monkeypatch.setattr(external_collectors, "_run_command_json", lambda *_args, **_kwargs: {"status": "ok", "secret": "must-not-persist"})
    monkeypatch.setattr(
        "fwrouter_api.services.external_connections_registry.mark_external_connection_seen",
        lambda connection_id, *, details=None: marked.append((connection_id, details)),
    )

    result = external_collectors.run_external_connection_collector("tailscale-test", dry_run=False)

    assert result["ok"] is True
    assert marked == [("tailscale-test", None)]
    assert "secret" not in str(result)


def test_dry_run_failed_and_non_interval_collectors_do_not_mark_seen(monkeypatch) -> None:
    marked: list[str] = []
    system = _connection()
    monkeypatch.setattr(external_collectors, "get_external_connection", lambda _connection_id: system)
    monkeypatch.setattr(external_collectors, "_run_command_json", lambda *_args, **_kwargs: {"status": "ok"})
    monkeypatch.setattr(
        "fwrouter_api.services.external_connections_registry.mark_external_connection_seen",
        lambda connection_id, **_kwargs: marked.append(connection_id),
    )

    dry_run = external_collectors.run_external_connection_collector("tailscale-test", dry_run=True)
    assert dry_run["ok"] is True
    assert marked == []

    system["refresh_mode"] = "on_change"
    non_interval = external_collectors.run_external_connection_collector("tailscale-test", dry_run=False)
    assert non_interval["ok"] is True
    assert marked == []

    system["refresh_mode"] = "interval"
    monkeypatch.setattr(external_collectors, "_run_command_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("probe failed")))
    failed = external_collectors.run_external_connection_collector("tailscale-test", dry_run=False)
    assert failed["ok"] is False
    assert marked == []


def test_skipped_push_collector_does_not_mark_seen(monkeypatch) -> None:
    marked: list[str] = []
    system = _connection()
    system["integration_mode"] = "api_push"
    monkeypatch.setattr(external_collectors, "get_external_connection", lambda _connection_id: system)
    monkeypatch.setattr(
        "fwrouter_api.services.external_connections_registry.mark_external_connection_seen",
        lambda connection_id, **_kwargs: marked.append(connection_id),
    )

    result = external_collectors.run_external_connection_collector("tailscale-test", dry_run=False)

    assert result["ok"] is True
    assert result["skipped"] is True
    assert marked == []
