"""Owned child-process interruption checks for registry origin publication."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/gates'))
import gate

FIXTURE = Path(__file__).with_name('restore_process_fixture.py')


def _environment(tmp_path):
    suite, _ = gate.create_suite_root(tmp_path, 'registry-origin-process')
    env = gate.clean_test_environment(suite)
    env['PYTHONPATH'] = str(ROOT / 'backend/tests') + os.pathsep + str(ROOT / 'backend')
    env['FWROUTER_PYTEST_COORDINATOR_ROOT'] = str(suite)
    env.pop('FWROUTER_GATE_NODE_REPORT', None)
    return suite, env


def _run(fixture_case, env):
    return subprocess.run([sys.executable, str(FIXTURE), fixture_case], env=env,
                          cwd=ROOT, capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize('boundary', ['pending', 'rename', 'readback', 'cleanup'])
def test_origin_publication_sigkill_and_fresh_process_recovery(tmp_path, boundary):
    _suite, env = _environment(tmp_path)
    killed = _run(f'registry_origin_crash_{boundary}', env)
    assert killed.returncode == -signal.SIGKILL, killed.stderr[-4096:]
    checked = _run('registry_origin_recover', env)
    assert checked.returncode == 0, checked.stderr[-4096:]
    assert json.loads(checked.stdout) == {'status': 'passed', 'origin_recovered': True}


@pytest.mark.parametrize('boundary', ['fence', 'selector'])
def test_cutover_boundary_sigkill_and_fresh_process_forward_completion(tmp_path, boundary):
    _suite, env = _environment(tmp_path)
    killed = _run(f'registry_origin_crash_{boundary}', env)
    assert killed.returncode == -signal.SIGKILL, killed.stderr[-4096:]
    checked = _run('registry_origin_recover_cutover', env)
    assert checked.returncode == 0, checked.stderr[-4096:]
    assert json.loads(checked.stdout) == {'status': 'passed', 'cutover_recovered': True}
