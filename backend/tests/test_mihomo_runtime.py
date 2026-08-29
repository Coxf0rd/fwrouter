from __future__ import annotations

from pathlib import Path

from fwrouter_api.services import mihomo_runtime


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
