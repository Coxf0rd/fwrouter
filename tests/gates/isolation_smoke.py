#!/usr/bin/env python3
"""Hosted-safe probe of the routine test bootstrap before application imports.

This validates the Python capability fence in a fresh child process. It is not
an application smoke, a native runtime check, or an OS-level sandbox test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "backend" / "tests" / "_isolation_bootstrap.py"
MAX_OUTPUT_BYTES = 8192


def run_probe() -> dict[str, Any]:
    if not BOOTSTRAP.is_file():
        return {"status": "failed", "reason": "bootstrap_missing", "checks": []}
    child = r'''
import importlib.util, json, os, socket, sqlite3, subprocess, sys
from pathlib import Path

bootstrap_path = Path(sys.argv[1])
os.environ["FWROUTER_STATE_DIR"] = "/opt/fwrouter-api/state"
os.environ["FWROUTER_PROBE_SECRET"] = "must-be-cleared"
os.environ["HOME"] = "/opt/fwrouter-api"
os.environ["HTTP_PROXY"] = "http://poison.invalid"
os.environ["HTTPS_PROXY"] = "http://poison.invalid"
os.environ["AWS_SECRET_ACCESS_KEY"] = "must-be-cleared"
os.environ["AWS_SESSION_TOKEN"] = "must-be-cleared"
external_root = Path(sys.argv[2])
protected_link = external_root / "protected"
protected_link.symlink_to("/var/lib/fwrouter-v2", target_is_directory=True)
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("fwrouter_isolation_bootstrap", bootstrap_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
owned_root, state_root = module.configure_test_process()
checks = []

def check(name, function):
    try:
        function()
    except Exception as exc:
        checks.append({"name": name, "status": "failed", "reason": type(exc).__name__})
    else:
        checks.append({"name": name, "status": "passed"})

def isolated_environment():
    assert os.environ.get("FWROUTER_ENVIRONMENT") == "test"
    assert os.environ.get("FWROUTER_STATE_DIR") == str(state_root)
    assert os.environ.get("HOME") == str(owned_root / "home")
    for name in ("FWROUTER_PROBE_SECRET", "HTTP_PROXY", "HTTPS_PROXY",
                 "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert name not in os.environ
    assert Path(owned_root, ".fwrouter-test-run-owned").is_file()

def owned_state_boundary():
    assert state_root == owned_root / "state"
    assert not module.path_is_protected(state_root / "fwrouter.db")
    assert module.path_is_owned(state_root)

def deployed_dotenv_denied():
    try:
        open("/opt/fwrouter-api/.env", encoding="utf-8").close()
    except PermissionError:
        return
    raise AssertionError("deployed dotenv was readable")

def arbitrary_dotenv_denied():
    try:
        open(external_root / ".env.probe", encoding="utf-8").close()
    except PermissionError:
        return
    raise AssertionError("non-example dotenv path was readable")

def root_credentials_denied():
    try:
        open("/root/.ssh/id_ed25519", encoding="utf-8").close()
    except PermissionError:
        return
    raise AssertionError("root credential path was readable")

def production_sqlite_denied():
    try:
        sqlite3.connect("/var/lib/fwrouter-v2/fwrouter.db")
    except PermissionError:
        return
    raise AssertionError("production SQLite was accessible")

def encoded_sqlite_uri_denied():
    try:
        sqlite3.connect("file:%2Fvar%2Flib%2Ffwrouter-v2%2Ffwrouter.db?mode=ro", uri=True)
    except PermissionError:
        return
    raise AssertionError("encoded production SQLite URI was accessible")

def protected_symlink_denied():
    try:
        open(protected_link / "fwrouter.db", "rb").close()
    except PermissionError:
        return
    raise AssertionError("protected path through symlink was accessible")

def subprocess_denied():
    try:
        subprocess.run(["true"], check=False)
    except PermissionError:
        return
    raise AssertionError("subprocess execution was allowed")

def internet_socket_denied():
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        return
    raise AssertionError("AF_INET socket was allowed")

def internet_v6_socket_denied():
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except PermissionError:
        return
    raise AssertionError("AF_INET6 socket was allowed")

def unix_connect_denied():
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            probe.connect(str(external_root / "missing.sock"))
        except PermissionError:
            return
        raise AssertionError("UNIX socket connect was allowed")
    finally:
        probe.close()

def dirfd_rename_denied():
    descriptor = os.open("/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.rename("fwrouter-source-missing", "fwrouter-target-missing",
                      src_dir_fd=descriptor, dst_dir_fd=descriptor)
        except PermissionError:
            return
        raise AssertionError("rename through an external dirfd was allowed")
    finally:
        os.close(descriptor)

def exec_denied():
    try:
        os.execv("/bin/true", ["true"])
    except PermissionError:
        return
    raise AssertionError("exec was allowed")

def unix_socketpair_allowed():
    left, right = socket.socketpair()
    left.close()
    right.close()

def owned_sqlite_allowed():
    path = state_root / "isolation-probe.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE probe (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO probe VALUES (1)")
        assert connection.execute("SELECT value FROM probe").fetchone() == (1,)

for name, function in (
    ("isolated_environment", isolated_environment),
    ("owned_state_boundary", owned_state_boundary),
    ("deployed_dotenv_denied", deployed_dotenv_denied),
    ("arbitrary_dotenv_denied", arbitrary_dotenv_denied),
    ("root_credentials_denied", root_credentials_denied),
    ("production_sqlite_denied", production_sqlite_denied),
    ("encoded_sqlite_uri_denied", encoded_sqlite_uri_denied),
    ("protected_symlink_denied", protected_symlink_denied),
    ("subprocess_denied", subprocess_denied),
    ("exec_denied", exec_denied),
    ("internet_socket_denied", internet_socket_denied),
    ("internet_v6_socket_denied", internet_v6_socket_denied),
    ("unix_connect_denied", unix_connect_denied),
    ("dirfd_rename_denied", dirfd_rename_denied),
    ("unix_socketpair_allowed", unix_socketpair_allowed),
    ("owned_sqlite_allowed", owned_sqlite_allowed),
):
    check(name, function)
print(json.dumps({"checks": checks, "owned_root": str(owned_root)}, sort_keys=True))
'''
    with tempfile.TemporaryDirectory(prefix="fwrouter-isolation-probe-", dir="/tmp") as outside:
        external_root = Path(outside)
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }
        result = subprocess.run(
            [sys.executable, "-I", "-c", child, str(BOOTSTRAP), outside],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=15, check=False, env=env,
        )
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        return {"status": "failed", "reason": "output_limit", "checks": []}
    if result.returncode:
        return {"status": "failed", "reason": "child_failed", "checks": []}
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "invalid_child_report", "checks": []}
    owned_root = payload.pop("owned_root", None)
    checks = payload.get("checks")
    if not isinstance(owned_root, str) or not isinstance(checks, list):
        return {"status": "failed", "reason": "invalid_child_report", "checks": []}
    cleanup = {"name": "owned_artifact_cleanup", "status": "passed" if not Path(owned_root).exists() else "failed"}
    checks.extend((cleanup, {
        "name": "probe_artifact_cleanup",
        "status": "passed" if not external_root.exists() else "failed",
    }))
    status = "passed" if all(row.get("status") == "passed" for row in checks) else "failed"
    return {"status": status, "checks": checks}


def main() -> int:
    result = run_probe()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
