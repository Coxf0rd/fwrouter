from __future__ import annotations

from fwrouter_api.core.config import Settings
from fwrouter_api.main import run
from fwrouter_api.services.event_contract import sanitize_value


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


def test_stealthsurf_empty_environment_values_allow_startup(monkeypatch) -> None:
    monkeypatch.setenv("FWROUTER_STEALTHSURF_API_KEY", "")
    monkeypatch.setenv("FWROUTER_STEALTHSURF_CONFIG_ID", "")

    settings = Settings(_env_file=None)

    assert settings.stealthsurf_api_key.get_secret_value() == ""
    assert settings.stealthsurf_config_id is None


def test_stealthsurf_values_are_configured_but_never_serialized_or_reprd(monkeypatch) -> None:
    api_key = "synthetic-stealthsurf-key-for-config-tests"
    monkeypatch.setenv("FWROUTER_STEALTHSURF_API_KEY", api_key)
    monkeypatch.setenv("FWROUTER_STEALTHSURF_CONFIG_ID", "731")

    settings = Settings(_env_file=None)

    assert settings.stealthsurf_api_key.get_secret_value() == api_key
    assert settings.stealthsurf_config_id == 731
    assert "stealthsurf_api_key" not in settings.model_dump()
    assert "stealthsurf_config_id" not in settings.model_dump()
    assert "stealthsurf_api_key" not in settings.model_dump_json()
    assert "stealthsurf_config_id" not in settings.model_dump_json()
    assert api_key not in repr(settings)
    assert "731" not in repr(settings)


def test_existing_api_key_and_bearer_redaction_remains_active() -> None:
    redacted = sanitize_value({
        "api_key": "synthetic-key-value",
        "authorization": "Bearer synthetic-bearer-value",
    })

    assert redacted == {
        "api_key": "[REDACTED]",
        "authorization": "[REDACTED]",
    }
