"""Fixed disposable process scenarios; launched only by the owned test coordinator."""
from __future__ import annotations
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
import fcntl
import uuid
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('_restore_process_bootstrap', ROOT / 'backend/tests/_isolation_bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
owned, state = bootstrap.configure_test_process()
from fwrouter_api.core.config import Settings, get_settings
Settings.model_config['env_file'] = None
get_settings.cache_clear()
from fwrouter_api.services import maintenance_lifecycle_admission as admission
admission._trusted_uid = os.getuid
case = sys.argv[1]

if case == 'lease_hold':
    # Only unit observations are synthetic; descriptors and kernel locks are real.
    admission._require_units_stopped = lambda: None
    host = Path(sys.argv[2])
    with admission.offline_rebuild_lease(int(sys.argv[3]), int(sys.argv[4]), state_root=host):
        (owned / 'ready').write_text('held')
        deadline = time.monotonic() + 8
        while not (owned / 'release').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('bounded fixture release timeout')
            time.sleep(.01)
    print(json.dumps({'status': 'passed'}))
    raise SystemExit(0)

from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import control_plane_transfer_import as owner
from fwrouter_api.services.control_plane_transfer_export import export_control_plane_snapshot
from fwrouter_api.services.rules import save_manual_draft, get_manual_rules_texts
from fwrouter_api import main
main.DEFAULT_STATE_ROOT = state
owner.get_scoped_egress_runtime_summary = lambda: {}
owner.build_system_summary = lambda: {}
from _test_support import maintenance_import_test_client
import pytest
patch = pytest.MonkeyPatch()

if case.startswith('offline_crash_') or case.startswith('offline_verify_'):
    from fwrouter_api.services import database_admin
    admission._require_units_stopped = lambda: None  # synthetic host observation
    import fwrouter_api.adapters.xray_common as xray_common
    xray_common._default_xray_config_path = lambda: str(owned / 'owned-xray' / 'config.json')
    database_admin.reconcile_control_plane_runtime = lambda **kwargs: {
        'subject_inventory': {'ok': True, 'synthetic': 'not_native_proof'}}
    directory = state / 'state' / 'controlled-rollout'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (state, state / 'state', directory):
        path.chmod(0o700)
    checkpoint = directory / 'pending.json'
    crashing = case.startswith('offline_crash_')
    phase = case.split('_', 2)[2]
    if crashing:
        initialize_database()
        snapshot = export_control_plane_snapshot(write_file=False)['snapshot']
        snapshot['state']['rules']['content']['manual_draft_text'] = 'DOMAIN,offline.example,VPN\n'
        operation_id = str(uuid.uuid4())
        (owned / 'offline-input.json').write_text(json.dumps({
            'snapshot': snapshot, 'operation_id': operation_id}))
        checkpoint.write_text(json.dumps({'operation_id': operation_id, 'status': 'pending',
            'maintenance_operation': {'kind': 'database_rebuild', 'status': 'pending', 'backup': None}}))
        checkpoint.chmod(0o600)
    else:
        prior = json.loads((owned / 'offline-input.json').read_text())
        snapshot, operation_id = prior['snapshot'], prior['operation_id']
        assert admission.startup_allowed(state) is False
        assert json.loads(checkpoint.read_text())['maintenance_operation']['status'] == phase
    first = os.open(directory / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(directory / '.maintenance.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with admission.offline_rebuild_lease(second, first, state_root=state):
            if crashing:
                advance = database_admin._offline_phase
                def kill_after_checkpoint(value):
                    advance(value)
                    if value == phase:
                        os.kill(os.getpid(), signal.SIGKILL)
                database_admin._offline_phase = kill_after_checkpoint
            result = database_admin.rebuild_control_plane_database(snapshot=snapshot, operation_id=operation_id)
            assert not crashing, 'fixture did not reach offline crash boundary'
            assert result['ok'] is True
            assert result['import']['replayed'] is True
            assert get_manual_rules_texts()['draft_text'] == 'DOMAIN,offline.example,VPN\n'
            assert len(list((state / 'backups').glob('*.bak'))) == 1
            assert admission.startup_allowed(state) is False
            assert json.loads(checkpoint.read_text())['maintenance_operation']['status'] == 'published'
            with closing(sqlite3.connect(
                    f"{(state / 'fwrouter.db').as_uri()}?mode=ro", uri=True)) as connection:
                assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert connection.execute('PRAGMA foreign_key_check').fetchone() is None
    finally:
        os.close(second)
        os.close(first)
    print(json.dumps({'status': 'passed', 'offline_restart_phase': phase,
                      'native_proof': 'not_run'}))
    raise SystemExit(0)

initialize_database()

def revision():
    with db_session() as c:
        row = c.execute("SELECT value_json FROM settings WHERE key='core.intent_revision'").fetchone()
    return row[0] if row else None

if case in {'crash_before', 'crash_after'}:
    save_manual_draft('DOMAIN,old.example,DIRECT\n')
    snapshot = export_control_plane_snapshot(write_file=False)['snapshot']
    snapshot['state']['rules']['content']['manual_draft_text'] = 'DOMAIN,new.example,VPN\n'
    (owned / 'input.json').write_text(json.dumps({'snapshot': snapshot, 'revision': revision()}))
    if case == 'crash_before':
        prepare = owner._write_rules_files_from_snapshot
        def die(rules):
            prepare(rules)
            os.kill(os.getpid(), signal.SIGKILL)
        owner._write_rules_files_from_snapshot = die
    else:
        owner.build_system_summary = lambda: os.kill(os.getpid(), signal.SIGKILL)
    with maintenance_import_test_client(patch, owned) as client:
        client.post('/api/v2/transfer/control-plane/import', json={'snapshot': snapshot, 'operation_id': '1' * 32})
    raise AssertionError('fixture did not reach crash boundary')
elif case in {'verify_before', 'verify_after'}:
    prior = json.loads((owned / 'input.json').read_text())
    committed = case == 'verify_after'
    assert get_manual_rules_texts()['draft_text'] == ('DOMAIN,new.example,VPN\n' if committed else 'DOMAIN,old.example,DIRECT\n')
    before = revision()
    if not committed:
        assert before == prior['revision']
    with maintenance_import_test_client(patch, owned) as client:
        result = client.post('/api/v2/transfer/control-plane/import', json={'snapshot': prior['snapshot'], 'operation_id': '1' * 32}).json()['data']['import']
    assert result['replayed'] is committed
    assert get_manual_rules_texts()['draft_text'] == 'DOMAIN,new.example,VPN\n'
    if committed:
        assert revision() == before
    print(json.dumps({'status': 'passed', 'crash_boundary': 'after_commit' if committed else 'before_commit'}))
else:
    raise RuntimeError('unknown fixed process scenario')
