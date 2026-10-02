from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fwrouter_api.adapters.xray_common import (
    xray_writer_guard,
    xray_writer_guard_is_held,
)


def test_writer_guard_is_reentrant_and_cleans_up_after_exception() -> None:
    assert not xray_writer_guard_is_held()

    with pytest.raises(ValueError, match="sentinel"):
        with xray_writer_guard():
            assert xray_writer_guard_is_held()
            with xray_writer_guard():
                assert xray_writer_guard_is_held()
                raise ValueError("sentinel")

    assert not xray_writer_guard_is_held()
    with xray_writer_guard():
        assert xray_writer_guard_is_held()
    assert not xray_writer_guard_is_held()


def test_writer_guard_serializes_threads_and_releases_after_timeout() -> None:
    entered = threading.Event()
    release = threading.Event()
    first_errors: list[BaseException] = []

    def hold_guard() -> None:
        try:
            with xray_writer_guard():
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release the first thread")
        except BaseException as exc:  # surfaced in the test thread
            first_errors.append(exc)

    first = threading.Thread(target=hold_guard, daemon=True)
    first.start()
    assert entered.wait(1)

    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="thread guard"):
            with xray_writer_guard(timeout_seconds=0.05):
                pytest.fail("second thread entered while the first held the guard")
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        first.join(2)

    assert not first.is_alive()
    assert not first_errors
    with xray_writer_guard(timeout_seconds=0.5):
        assert xray_writer_guard_is_held()


_FLOCK_HOLDER = r"""
import fcntl
import os
import sys
import time

lock_path, ready_path, hold_seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
with open(ready_path, "w", encoding="utf-8") as ready:
    ready.write("ready")
time.sleep(hold_seconds)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""

_GUARD_CONTENDER = r"""
import sys
from fwrouter_api.adapters.xray_common import xray_writer_guard

timeout = float(sys.argv[1])
try:
    with xray_writer_guard(timeout_seconds=timeout):
        print("acquired", flush=True)
except TimeoutError as exc:
    print(f"timeout:{exc}", flush=True)
"""


def _isolated_env(state_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["FWROUTER_STATE_DIR"] = str(state_dir)
    env["FWROUTER_ENVIRONMENT"] = "test"
    return env


def _start_flock_holder(run_dir: Path, *, hold_seconds: float) -> subprocess.Popen[str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    ready_path = run_dir / "holder-ready"
    ready_path.unlink(missing_ok=True)
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _FLOCK_HOLDER,
            str(run_dir / "xray-writer.lock"),
            str(ready_path),
            str(hold_seconds),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=_isolated_env(run_dir.parent),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_file(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 2
    while not path.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"flock holder exited early: {stdout}\n{stderr}")
        time.sleep(0.01)
    assert path.exists(), "flock holder did not signal lock acquisition"


def _run_guard_contender(state_dir: Path, *, timeout: float) -> tuple[str, float]:
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", _GUARD_CONTENDER, str(timeout)],
        cwd=Path(__file__).resolve().parents[1],
        env=_isolated_env(state_dir),
        text=True,
        capture_output=True,
        timeout=3,
        check=True,
    )
    return result.stdout.strip(), time.monotonic() - started


def test_writer_guard_serializes_processes_and_cleans_up_after_flock_timeout(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    run_dir = state_dir / "run"
    holder = _start_flock_holder(run_dir, hold_seconds=0.4)
    try:
        _wait_for_file(run_dir / "holder-ready", holder)
        outcome, _ = _run_guard_contender(state_dir, timeout=0.08)
        assert outcome.startswith("timeout:Timed out waiting for the Xray writer process guard.")

        holder.communicate(timeout=2)
        assert holder.returncode == 0

        outcome, _ = _run_guard_contender(state_dir, timeout=0.5)
        assert outcome == "acquired"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.communicate(timeout=2)


def test_writer_guard_uses_one_timeout_budget_for_thread_and_process_waits(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    run_dir = state_dir / "run"
    holder = _start_flock_holder(run_dir, hold_seconds=0.9)
    thread_lock_acquired = threading.Event()
    release_thread_lock = threading.Event()

    def hold_thread_lock() -> None:
        from fwrouter_api.adapters.xray_common import _XRAY_WRITER_THREAD_LOCK

        _XRAY_WRITER_THREAD_LOCK.acquire()
        try:
            thread_lock_acquired.set()
            release_thread_lock.wait(2)
        finally:
            _XRAY_WRITER_THREAD_LOCK.release()

    lock_holder = threading.Thread(target=hold_thread_lock, daemon=True)
    lock_holder.start()
    try:
        assert thread_lock_acquired.wait(1)
        _wait_for_file(run_dir / "holder-ready", holder)
        release = threading.Timer(0.25, release_thread_lock.set)
        release.start()
        try:
            started = time.monotonic()
            with pytest.raises(TimeoutError, match="process guard"):
                with xray_writer_guard(timeout_seconds=0.5):
                    pytest.fail("guard acquired despite the held process lock")
            elapsed = time.monotonic() - started
        finally:
            release.cancel()
            release_thread_lock.set()
            lock_holder.join(2)

        assert elapsed >= 0.45
        # One budget expires near 0.5s; resetting after the 0.25s thread wait
        # would allow the process wait to run for another 0.5s (about 0.75s total).
        assert elapsed < 0.63
        assert not lock_holder.is_alive()
        assert not xray_writer_guard_is_held()

        holder.communicate(timeout=2)
        assert holder.returncode == 0
        with xray_writer_guard(timeout_seconds=0.5):
            assert xray_writer_guard_is_held()
    finally:
        release_thread_lock.set()
        if lock_holder.is_alive():
            lock_holder.join(2)
        if holder.poll() is None:
            holder.terminate()
            holder.communicate(timeout=2)
