from __future__ import annotations

import base64
import io
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from fwrouter_api.adapters import xray_real
from fwrouter_api.adapters.xray_real import RealXrayAdapter


def _tar_config(payload: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("config.json")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def _install_real_child_popen(monkeypatch, process_holder: list[subprocess.Popen], script_factory):
    real_popen = subprocess.Popen

    def run_child(command, **kwargs):
        assert command == ["docker", "cp", f"{'a' * 64}:/etc/xray/config.json", "-"]
        process = real_popen([sys.executable, "-c", script_factory()], **kwargs)
        process_holder.append(process)
        return process

    monkeypatch.setattr(xray_real.subprocess, "Popen", run_child)


def _adapter(tmp_path: Path) -> RealXrayAdapter:
    return RealXrayAdapter(
        config_path=tmp_path / "host-config.json",
        compose_path=tmp_path / "compose.yml",
    )


def test_default_runner_reads_archive_with_real_popen_and_selector(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(xray_real, "DOCKER_CLI_STATE_DIR", tmp_path / "docker-cli")
    process_holder: list[subprocess.Popen] = []
    expected = _tar_config(b'{"inbounds":[]}')
    encoded = base64.b64encode(expected).decode("ascii")
    _install_real_child_popen(
        monkeypatch, process_holder,
        lambda: f"import base64,sys;sys.stdout.buffer.write(base64.b64decode('{encoded}'))",
    )

    result = _adapter(tmp_path)._default_runner(
        "runtime_config_archive", {"container_id": "a" * 64},
    )

    assert result.ok is True
    assert result.details["archive_bytes"] == expected
    assert process_holder and process_holder[0].poll() is not None
    assert (tmp_path / "docker-cli").is_dir()


def test_default_runner_archive_timeout_kills_child(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(xray_real, "DOCKER_CLI_STATE_DIR", tmp_path / "docker-cli")
    ticks = iter((100.0, 106.0))
    monkeypatch.setattr(xray_real, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    process_holder: list[subprocess.Popen] = []
    _install_real_child_popen(
        monkeypatch, process_holder,
        lambda: "import time;time.sleep(60)",
    )

    result = _adapter(tmp_path)._default_runner(
        "runtime_config_archive", {"container_id": "a" * 64},
    )

    assert result.ok is False
    assert result.error_code == "XRAY_RUNTIME_READBACK_TIMEOUT"
    assert process_holder and process_holder[0].poll() is not None


def test_default_runner_archive_size_limit_kills_child(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(xray_real, "DOCKER_CLI_STATE_DIR", tmp_path / "docker-cli")
    process_holder: list[subprocess.Popen] = []
    _install_real_child_popen(
        monkeypatch, process_holder,
        lambda: "import sys;sys.stdout.buffer.write(b'x' * (5 * 1024 * 1024))",
    )

    result = _adapter(tmp_path)._default_runner(
        "runtime_config_archive", {"container_id": "a" * 64},
    )

    assert result.ok is False
    assert result.error_code == "XRAY_RUNTIME_CONFIG_TOO_LARGE"
    assert process_holder and process_holder[0].poll() is not None
