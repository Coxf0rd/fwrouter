from __future__ import annotations

import socket
import sqlite3
import subprocess
import os
from pathlib import Path

import pytest

from _fwrouter_test_isolation_bootstrap import OWNED_ROOT, STATE_ROOT, path_is_protected
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


def test_bootstrap_allows_sqlite_only_inside_owned_state() -> None:
    assert STATE_ROOT is not None
    db_path = Path(STATE_ROOT) / "bootstrap-probe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE probe (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO probe (value) VALUES (1)")
        assert connection.execute("SELECT value FROM probe").fetchone() == (1,)
