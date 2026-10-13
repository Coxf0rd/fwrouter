from __future__ import annotations

from pathlib import Path

from fwrouter_api.services import mihomo_runtime
from fwrouter_api.adapters.mihomo import MihomoHttpAdapter


def test_mihomo_compose_probe_uses_fwrouter_docker_cli_state(monkeypatch, tmp_path: Path) -> None:
    docker_cli_state = tmp_path / "run" / "docker-cli"
    compose_file = tmp_path / "docker-compose.yml"
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return mihomo_runtime.subprocess.CompletedProcess(
            command,
            0,
            stdout="NAME              STATUS\nfwrouter-mihomo   running\n",
            stderr="",
        )

    monkeypatch.setattr(mihomo_runtime, "DOCKER_CLI_STATE_DIR", docker_cli_state)
    monkeypatch.setattr(mihomo_runtime, "MIHOMO_COMPOSE_FILE", Path(compose_file))
    monkeypatch.setattr(mihomo_runtime.subprocess, "run", fake_run)

    result = mihomo_runtime.get_mihomo_container_status()

    assert result["ok"] is True
    assert docker_cli_state.exists()
    assert captured["command"] == [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "ps",
        "mihomo",
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_CONFIG"] == str(docker_cli_state)
    assert env["HOME"] == str(docker_cli_state)


def test_mihomo_controller_readiness_only_checks_version(monkeypatch, tmp_path: Path) -> None:
    adapter = MihomoHttpAdapter(config_path=tmp_path / "large-config.yaml")
    captured: dict[str, object] = {}

    def fake_get_json(path: str, *, timeout_seconds: float | None = None):
        captured["path"] = path
        captured["timeout_seconds"] = timeout_seconds
        return {"version": "1.19.0"}

    monkeypatch.setattr(adapter, "_get_json", fake_get_json)

    assert adapter.controller_ready(timeout_seconds=0.75) == {"ok": True, "version": "1.19.0"}
    assert captured == {"path": "/version", "timeout_seconds": 0.75}


def test_mihomo_controller_readiness_rejects_malformed_version(monkeypatch, tmp_path: Path) -> None:
    adapter = MihomoHttpAdapter(config_path=tmp_path / "large-config.yaml")
    monkeypatch.setattr(adapter, "_get_json", lambda *_args, **_kwargs: {"version": "  "})

    try:
        adapter.controller_ready(timeout_seconds=0.5)
    except ValueError as exc:
        assert "invalid version response" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("malformed version response must not count as ready")


def test_wait_for_mihomo_controller_does_not_run_full_health(monkeypatch) -> None:
    calls: list[float] = []

    class ReadinessOnlyAdapter:
        timeout_seconds = 3.0

        def controller_ready(self, *, timeout_seconds: float):
            calls.append(timeout_seconds)
            return {"ok": True, "version": "1.19.0"}

        def health(self):  # pragma: no cover - must remain unused
            raise AssertionError("controller readiness must not run full policy health")

    monkeypatch.setattr(mihomo_runtime, "DEFAULT_MIHOMO_ADAPTER", ReadinessOnlyAdapter())
    result = mihomo_runtime.wait_for_mihomo_controller(timeout_seconds=2.0)

    assert result["ok"] is True
    assert result["policy_proven"] is False
    assert result["readiness"] == {"ok": True, "version": "1.19.0"}
    assert len(calls) == 1
    assert 0 < calls[0] <= 2.0


def test_wait_for_mihomo_controller_retries_with_deadline_bounded_sleep(monkeypatch) -> None:
    now = [0.0]
    timeouts: list[float] = []
    sleeps: list[float] = []

    class UnreachableAdapter:
        timeout_seconds = 3.0

        def controller_ready(self, *, timeout_seconds: float):
            timeouts.append(timeout_seconds)
            raise OSError("connection refused")

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(mihomo_runtime, "DEFAULT_MIHOMO_ADAPTER", UnreachableAdapter())
    monkeypatch.setattr(mihomo_runtime.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mihomo_runtime.time, "sleep", fake_sleep)

    result = mihomo_runtime.wait_for_mihomo_controller(timeout_seconds=1.0, poll_interval_seconds=0.5)

    assert result["ok"] is False
    assert result["policy_proven"] is False
    assert result["attempts"] == 2
    assert timeouts == [1.0, 0.5]
    assert sleeps == [0.5, 0.5]
    assert sum(sleeps) == 1.0
    assert result["readiness"] == {"ok": False, "error_type": "OSError"}


def test_mihomo_restart_still_fails_when_selector_restore_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        mihomo_runtime,
        "_run_compose_command",
        lambda *_args, **_kwargs: {"ok": True, "returncode": 0, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        mihomo_runtime,
        "get_mihomo_container_status",
        lambda: {"ok": True, "returncode": 0, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        mihomo_runtime,
        "wait_for_mihomo_controller",
        lambda **_kwargs: {"ok": True, "readiness": {"ok": True}, "policy_proven": False},
    )
    selector_restore = {"ok": False, "runtime_state": "degraded"}
    monkeypatch.setattr(
        "fwrouter_api.services.selector.restore_mihomo_selector_state",
        lambda **_kwargs: selector_restore,
    )

    result = mihomo_runtime._restart_mihomo_container(action="restart")

    assert result["controller_wait"]["ok"] is True
    assert result["controller_wait"]["policy_proven"] is False
    assert result["selector_restore"] == selector_restore
    assert result["ok"] is False
