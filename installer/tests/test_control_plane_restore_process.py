"""Real owned child processes; no live systemd, native runtime or production DB."""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/gates'))
import gate
FIXTURE = Path(__file__).with_name('restore_process_fixture.py')


def _environment(tmp_path):
    suite, _ = gate.create_suite_root(tmp_path, 'restore-process')
    env = gate.clean_test_environment(suite)
    env['PYTHONPATH'] = str(ROOT/'backend/tests') + os.pathsep + str(ROOT/'backend')
    env['FWROUTER_PYTEST_COORDINATOR_ROOT'] = str(suite)
    env.pop('FWROUTER_GATE_NODE_REPORT', None)
    return suite, env


@pytest.mark.parametrize('committed', [False, True])
def test_real_sigkill_import_and_fresh_process_retry(tmp_path, committed):
    suite, env = _environment(tmp_path)
    stage = 'after' if committed else 'before'
    killed = subprocess.run([sys.executable, str(FIXTURE), 'crash_'+stage], env=env,
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert killed.returncode == -signal.SIGKILL, killed.stderr[-4096:]
    checked = subprocess.run([sys.executable, str(FIXTURE), 'verify_'+stage], env=env,
                             cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr[-4096:]
    assert json.loads(checked.stdout)['status'] == 'passed'
    assert (suite/'input.json').exists()


@pytest.mark.parametrize('stage', ['before', 'after'])
def test_real_sigkill_job_terminal_event_commit_and_retention(tmp_path, stage):
    suite, env = _environment(tmp_path)
    killed = subprocess.run([sys.executable, str(FIXTURE), 'terminal_crash_' + stage], env=env,
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert killed.returncode == -signal.SIGKILL, killed.stderr[-4096:]
    checked = subprocess.run([sys.executable, str(FIXTURE), 'terminal_verify_' + stage], env=env,
                             cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr[-4096:]
    result = json.loads(checked.stdout)
    assert result['status'] == 'passed'
    assert result['crash_boundary'] == ('before_commit' if stage == 'before' else 'after_commit')
    assert result['retained_snapshots'] == 1


def test_inherited_lifecycle_and_exclusive_fd_survive_controller_close(tmp_path):
    suite, env = _environment(tmp_path)
    host = suite/'host'
    directory = host/'state/controlled-rollout'
    directory.mkdir(parents=True, mode=0o700)
    for path in (host, host/'state', directory): path.chmod(0o700)
    checkpoint = directory/'pending.json'
    checkpoint.write_text(json.dumps({'operation_id': str(uuid.uuid4()), 'status': 'pending',
        'maintenance_operation': {'kind': 'database_rebuild', 'status': 'pending', 'backup': None}}))
    checkpoint.chmod(0o600)
    lock = directory/'.lock'
    lease = directory/'.maintenance.lock'
    first = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(lease, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
    child = subprocess.Popen([sys.executable,str(FIXTURE),'lease_hold',str(host),str(second),str(first)],
                             env=env,cwd=ROOT,pass_fds=(first,second),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline = time.monotonic()+8
        while not (suite/'ready').exists():
            assert child.poll() is None, child.communicate(timeout=1)
            assert time.monotonic() < deadline
            time.sleep(.01)
        os.close(first);first = None
        os.close(second);second = None
        for path in (lock,lease):
            probe=os.open(path,os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError): fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally: os.close(probe)
        (suite/'release').write_text('release')
        stdout,stderr=child.communicate(timeout=10)
        assert child.returncode == 0, stderr[-4096:]
        assert json.loads(stdout)['status']=='passed'
        for path in (lock,lease):
            probe=os.open(path,os.O_RDWR)
            try: fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally: os.close(probe)
    finally:
        if first is not None:os.close(first)
        if second is not None:os.close(second)
        if child.poll() is None:child.kill();child.communicate(timeout=3)


def test_shared_lease_fd_survives_controller_sigkill_until_preflight_child_exits(tmp_path):
    suite, env = _environment(tmp_path)
    host = suite / 'host'
    directory = host / 'state' / 'controlled-rollout'
    directory.mkdir(parents=True, mode=0o700)
    for path in (host, host / 'state', directory):
        path.chmod(0o700)
    lease_path = directory / '.maintenance.lock'
    lease_fd = os.open(lease_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lease_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)

    holder_script = r"""
import fcntl, os, sys, time
from pathlib import Path
fd, ready, release = int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
ready.write_text('held')
deadline = time.monotonic() + 8
while not release.exists():
    if time.monotonic() >= deadline:
        raise SystemExit('bounded holder release timeout')
    time.sleep(.01)
os.close(fd)
"""
    controller_script = r"""
import os, subprocess, sys, time
from pathlib import Path
fd = int(sys.argv[1])
holder, ready, release, controller_ready = map(Path, sys.argv[2:6])
child = subprocess.Popen(
    [sys.executable, '-c', holder.read_text(), str(fd), str(ready), str(release)],
    pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, close_fds=True,
)
deadline = time.monotonic() + 8
while not ready.exists():
    if child.poll() is not None or time.monotonic() >= deadline:
        raise SystemExit('bounded holder startup failed')
    time.sleep(.01)
controller_ready.write_text('ready')
deadline = time.monotonic() + 8
while not release.exists() and child.poll() is None and time.monotonic() < deadline:
    time.sleep(.05)
if not release.exists():
    release.touch()
try:
    child.wait(timeout=2)
except subprocess.TimeoutExpired:
    child.kill()
    child.wait(timeout=2)
"""
    holder_path = suite / 'lease-holder.py'
    controller_path = suite / 'lease-controller.py'
    ready = suite / 'holder-ready'
    release = suite / 'holder-release'
    controller_ready = suite / 'controller-ready'
    holder_path.write_text(holder_script)
    controller_path.write_text(controller_script)
    holder_path.chmod(0o600)
    controller_path.chmod(0o600)
    controller = subprocess.Popen(
        [sys.executable, str(controller_path), str(lease_fd), str(holder_path), str(ready),
         str(release), str(controller_ready)],
        env=env, cwd=ROOT, pass_fds=(lease_fd,), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 8
        while not controller_ready.exists():
            assert controller.poll() is None, 'lease controller exited before holder readiness'
            assert time.monotonic() < deadline, 'bounded controller startup timeout'
            time.sleep(.01)
        assert ready.read_text() == 'held'
        os.close(lease_fd)
        lease_fd = -1
        controller.kill()
        assert controller.wait(timeout=3) == -signal.SIGKILL

        probe = os.open(lease_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

        release.write_text('release')
        deadline = time.monotonic() + 8
        while True:
            probe = os.open(lease_path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
            finally:
                os.close(probe)
            assert time.monotonic() < deadline, 'holder did not release shared lease'
            time.sleep(.01)
    finally:
        if lease_fd >= 0:
            os.close(lease_fd)
        release.touch(exist_ok=True)
        try:
            os.killpg(controller.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            controller.wait(timeout=3)
        except subprocess.TimeoutExpired:
            controller.kill()
            controller.wait(timeout=3)


@pytest.mark.parametrize('phase', ['candidate_ready', 'source_quiesced', 'published'])
def test_real_sigkill_offline_rebuild_and_fresh_process_forward_retry(tmp_path, phase):
    suite, env = _environment(tmp_path)
    killed = subprocess.run([sys.executable, str(FIXTURE), 'offline_crash_' + phase],
                            env=env, cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert killed.returncode == -signal.SIGKILL, killed.stderr[-4096:]
    checked = subprocess.run([sys.executable, str(FIXTURE), 'offline_verify_' + phase],
                             env=env, cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr[-4096:]
    result = json.loads(checked.stdout)
    assert result['status'] == 'passed'
    assert result['offline_restart_phase'] == phase
    assert result['native_proof'] == 'not_run'
    assert (suite / 'offline-input.json').exists()
