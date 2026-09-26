from __future__ import annotations

from fwrouter_api.core.config import Settings
from fwrouter_api.main import run


def test_removed_environment_keys_are_not_settings(monkeypatch) -> None:
    monkeypatch.setenv("FWROUTER_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("FWROUTER_BIND_PORT", "9000")
    monkeypatch.setenv("FWROUTER_DATABASE_URL", "sqlite:///unused.db")
    settings = Settings(_env_file=None)

    assert not hasattr(settings, "bind_host")
    assert not hasattr(settings, "bind_port")
    assert not hasattr(settings, "database_url")
    assert not hasattr(settings, "paths_override")


def test_launcher_uses_fixed_loopback_binding(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(*args, **kwargs) -> None:
        observed.update(kwargs)

    monkeypatch.setattr("fwrouter_api.main.uvicorn.run", fake_run)
    run()

    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 5000
