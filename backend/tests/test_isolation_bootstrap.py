from __future__ import annotations

from contextlib import closing
import socket
import sqlite3
import subprocess
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from _fwrouter_test_isolation_bootstrap import OWNED_ROOT, STATE_ROOT, path_is_protected
import _fwrouter_test_isolation_bootstrap as isolation_bootstrap
from fwrouter_api.core.config import Settings


def test_bootstrap_uses_owned_state_and_disables_deployed_dotenv() -> None:
    assert OWNED_ROOT is not None
    assert STATE_ROOT is not None
    assert Settings.model_config.get("env_file") is None
    current_state = Path(os.environ["FWROUTER_STATE_DIR"])
    assert current_state.is_relative_to(OWNED_ROOT)
    assert Settings().paths.state_dir.resolve() == current_state.resolve()
    assert Settings().paths.run_dir.resolve() == (current_state / "run").resolve()
    assert not path_is_protected(current_state / "fwrouter.db")
    assert not path_is_protected(current_state / "run" / "xray-writer.lock")


def test_bootstrap_denies_production_paths_processes_and_network() -> None:
    with pytest.raises(PermissionError, match="outside owned state"):
        with open("/opt/fwrouter-api/.env", encoding="utf-8"):
            pass

    with pytest.raises(PermissionError, match="SQLite outside owned state"):
        sqlite3.connect("/var/lib/fwrouter-v2/fwrouter.db")

    with pytest.raises(PermissionError, match="process execution"):
        subprocess.run(["true"], check=False)

    with pytest.raises(PermissionError, match="socket capability"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    left, right = socket.socketpair()
    left.close()
    right.close()


def test_bootstrap_allows_sqlite_only_inside_owned_state(tmp_path: Path) -> None:
    assert STATE_ROOT is not None
    db_path = tmp_path / "bootstrap-probe.sqlite"
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE probe (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO probe (value) VALUES (1)")
        connection.commit()
        assert connection.execute("SELECT value FROM probe").fetchone() == (1,)


def test_isolated_host_observations_keep_external_source_probe_unavailable(
    isolated_host_observations,
) -> None:
    from fwrouter_api.services.external_source_observations import read_external_source_observations

    observation = read_external_source_observations("tailscale")

    assert observation["ok"] is False
    assert observation["error_code"] == "EXTERNAL_SOURCE_PROBE_UNAVAILABLE"
    assert observation["items"] == []
    assert observation["local_identities"] == []
    assert observation["by_subject_id"] == {}


def test_qualified_docker_xray_copy_requires_inspected_reserved_container_and_owned_destination(
    tmp_path: Path,
) -> None:
    container_name = "native-xray-readback-test-0123456789ab"
    container_id = "a" * 64
    profile = {"container_name": container_name, "suite_nonce": "0123456789abcdef" * 2}
    command = ["docker", "cp", f"{container_id}:/etc/xray/config.json", str(tmp_path / "config.json")]
    inspected = SimpleNamespace(stdout=f"{container_id}\n")

    with (mock.patch.object(isolation_bootstrap, "QUALIFIED_DOCKER_XRAY_PROFILE", profile),
          mock.patch.object(isolation_bootstrap, "QUALIFIED_DOCKER_XRAY_FILE", "/workspace/backend/tests/test_xray_native_readback.py"),
          mock.patch.object(isolation_bootstrap, "_qualified_child_test_file",
                            return_value="/workspace/backend/tests/test_xray_native_readback.py"),
          mock.patch.object(subprocess, "run", return_value=inspected) as run):
        assert isolation_bootstrap._qualified_docker_xray_process_allowed((None, command, None, None))
        run.assert_called_once_with(
            ["docker", "inspect", "--format", "{{.Id}}", container_name],
            check=True, capture_output=True, text=True, timeout=5,
        )

        foreign_id_command = ["docker", "cp", f"{'b' * 64}:/etc/xray/config.json", str(tmp_path / "config.json")]
        assert not isolation_bootstrap._qualified_docker_xray_process_allowed(
            (None, foreign_id_command, None, None),
        )
        wrong_name_command = ["docker", "cp", "native-xray-readback-test-other:/etc/xray/config.json",
                              str(tmp_path / "config.json")]
        assert not isolation_bootstrap._qualified_docker_xray_process_allowed(
            (None, wrong_name_command, None, None),
        )
        wrong_source_command = ["docker", "cp", f"{container_id}:/etc/xray/other.json", str(tmp_path / "config.json")]
        assert not isolation_bootstrap._qualified_docker_xray_process_allowed(
            (None, wrong_source_command, None, None),
        )
        wrong_destination_command = ["docker", "cp", f"{container_id}:/etc/xray/config.json",
                                     "/etc/fwrouter/config.json"]
        assert not isolation_bootstrap._qualified_docker_xray_process_allowed(
            (None, wrong_destination_command, None, None),
        )
