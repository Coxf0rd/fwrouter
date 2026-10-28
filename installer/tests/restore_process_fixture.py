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
case = sys.argv[1]

if case.startswith('terminal_crash_') or case.startswith('terminal_verify_'):
    from contextlib import contextmanager
    from datetime import timedelta
    from fwrouter_api.core.config import Settings, get_settings
    Settings.model_config['env_file'] = None
    get_settings.cache_clear()
    from fwrouter_api.db.connection import db_session, initialize_database
    from fwrouter_api.services import jobs

    initialize_database()
    metadata = owned / 'terminal-job.json'
    if case.startswith('terminal_crash_'):
        stage = case.removeprefix('terminal_crash_')
        artifact_dir = get_settings().paths.jobs_dir / 'terminal-process-probe'
        artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        job = jobs.create_job(
            'terminal_process_probe', artifact_dir=str(artifact_dir),
            event_context={'request_id': 'persisted-process-request'},
        )
        job_id = job['job_id']
        jobs.mark_job_running(job_id)
        metadata.write_text(json.dumps({'job_id': job_id, 'artifact_dir': str(artifact_dir)}))
        if stage == 'before':
            original_write = jobs.write_audit_event

            def kill_after_event_insert(*args, **kwargs):
                original_write(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGKILL)

            jobs.write_audit_event = kill_after_event_insert
        elif stage == 'after':
            original_session = jobs.db_session

            @contextmanager
            def kill_after_terminal_commit():
                with original_session() as connection:
                    yield connection
                os.kill(os.getpid(), signal.SIGKILL)

            jobs.db_session = kill_after_terminal_commit
        else:
            raise RuntimeError('unknown terminal crash stage')
        jobs.mark_job_success(job_id, result={'ok': True})
        raise AssertionError('terminal job crash hook was not reached')

    stage = case.removeprefix('terminal_verify_')
    prior = json.loads(metadata.read_text())
    job_id = prior['job_id']
    artifact_dir = Path(prior['artifact_dir'])
    event_id = jobs._terminal_snapshot_event_id(job_id)
    with db_session() as connection:
        job_row = connection.execute('SELECT * FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
        event_row = connection.execute('SELECT * FROM operational_logs WHERE event_id = ?', (event_id,)).fetchone()
    if stage == 'before':
        assert job_row['status'] == 'running'
        assert event_row is None
        recovered = jobs.cleanup_stale_running_jobs(stale_after_seconds=0)
        assert len(recovered) == 1 and recovered[0]['status'] == 'failed'
        jobs.mark_job_success(job_id, result={'retry_must_not_win': True})
        expected_status = 'failed'
        retention_age_days = 15
    elif stage == 'after':
        assert job_row['status'] == 'success'
        assert event_row is not None
        jobs.cleanup_stale_running_jobs(stale_after_seconds=0)
        jobs.mark_job_failed(job_id, error_code='retry_must_not_win', error_message='retry must not win')
        expected_status = 'success'
        retention_age_days = 2
    else:
        raise RuntimeError('unknown terminal verification stage')

    with db_session() as connection:
        job_row = connection.execute('SELECT * FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
        event_row = connection.execute('SELECT * FROM operational_logs WHERE event_id = ?', (event_id,)).fetchone()
        assert job_row['status'] == expected_status
        assert event_row is not None
        details = json.loads(event_row['details_json'])
        assert details['execution_status'] == expected_status
        assert details['snapshot_assurance'] == 'terminal_commit'
        assert details['outcome'] == expected_status
        assert details['event_context']['job_id'] == job_id
        assert details['event_context']['request_id'] == 'persisted-process-request'
        immutable_details = event_row['details_json']

    from fwrouter_api.services import jobs_retention
    cleanup_jobs_retention = jobs_retention.cleanup_jobs_retention
    observed_now = jobs_retention._utc_now()
    jobs_retention._utc_now = lambda: observed_now + timedelta(days=retention_age_days + 1)
    result = cleanup_jobs_retention(dry_run=False)
    assert job_id in result['deleted_jobs'] and not result['errors']
    assert not artifact_dir.exists()
    result = cleanup_jobs_retention(dry_run=False)
    assert result['deleted_jobs_count'] == 0
    with db_session() as connection:
        assert connection.execute('SELECT 1 FROM jobs WHERE job_id = ?', (job_id,)).fetchone() is None
        retained = connection.execute(
            'SELECT event_id, details_json FROM operational_logs WHERE event_id = ?', (event_id,)
        ).fetchone()
        assert retained is not None
        assert retained['event_id'] == event_id
        assert retained['details_json'] == immutable_details
    print(json.dumps({'status': 'passed', 'crash_boundary': 'before_commit' if stage == 'before' else 'after_commit',
                      'retained_snapshots': 1}))
    raise SystemExit(0)

if case.startswith('registry_origin_'):
    from fwrouter_api.services import api_client_registry as registry
    from fwrouter_api.services import api_client_registry_admin as admin
    from types import SimpleNamespace
    host = owned / 'registry-host'
    host.mkdir(mode=0o700, exist_ok=True)
    host.chmod(0o700)
    for name in ('REGISTRY_PATH', 'SELECTOR_PATH', 'FENCE_PATH', 'PENDING_PATH', 'LOCK_PATH'):
        setattr(registry, name, host / {
            'REGISTRY_PATH': 'api-clients.json',
            'SELECTOR_PATH': 'presentation-client-list-verifier.json',
            'FENCE_PATH': 'api-clients.required',
            'PENDING_PATH': 'api-clients.pending',
            'LOCK_PATH': 'api-clients.lock',
        }[name])
    registry._trusted_uid = os.getuid

    def setup():
        admin._run(SimpleNamespace(command='init', canonical_origin='https://router.example.test'))
        for revision, kind in enumerate(('web_ui', 'cli'), start=1):
            admin._run(SimpleNamespace(command='create', expected_revision=revision,
                kind=kind, tag=kind, scope=['ui.client-list.write'],
                secret_file=str(host / f'{kind}.token')))

    if case in {'registry_origin_crash_pending', 'registry_origin_crash_rename',
                'registry_origin_crash_readback', 'registry_origin_crash_cleanup'}:
        admin._run(SimpleNamespace(command='init', canonical_origin='https://router.example.test'))
        original_readback = registry._durable_readback
        original_replace = registry.os.replace
        original_cleanup = registry._clear_pending

        if case.endswith('_pending'):
            def readback(path, expected):
                original_readback(path, expected)
                if path == registry.PENDING_PATH:
                    os.kill(os.getpid(), signal.SIGKILL)
            registry._durable_readback = readback
        elif case.endswith('_rename'):
            def replace(source, target, **kwargs):
                original_replace(source, target, **kwargs)
                if str(target).endswith('/api-clients.json'):
                    os.kill(os.getpid(), signal.SIGKILL)
            registry.os.replace = replace
        elif case.endswith('_readback'):
            def readback(path, expected):
                original_readback(path, expected)
                if path == registry.REGISTRY_PATH:
                    os.kill(os.getpid(), signal.SIGKILL)
            registry._durable_readback = readback
        else:
            def cleanup(path):
                original_cleanup(path)
                os.kill(os.getpid(), signal.SIGKILL)
            registry._clear_pending = cleanup
        admin._run(SimpleNamespace(command='set-origin', expected_revision=1,
                                   canonical_origin='https://new.example.test'))
        raise AssertionError('origin crash hook was not reached')

    if case in {'registry_origin_crash_fence', 'registry_origin_crash_selector'}:
        setup()
        original_publish = registry._atomic_publish
        original_replace = registry.os.replace
        if case.endswith('_fence'):
            def publish(path, data, **kwargs):
                result = original_publish(path, data, **kwargs)
                if path == registry.FENCE_PATH:
                    os.kill(os.getpid(), signal.SIGKILL)
                return result
            registry._atomic_publish = publish
        else:
            def replace(source, target, **kwargs):
                original_replace(source, target, **kwargs)
                if str(target).endswith('presentation-client-list-verifier.json'):
                    os.kill(os.getpid(), signal.SIGKILL)
            registry.os.replace = replace
        admin._run(SimpleNamespace(command='cutover', expected_revision=3))
        raise AssertionError('cutover crash hook was not reached')

    if case == 'registry_origin_recover':
        pending = registry._pending_state()
        if pending is not None:
            admin._run(SimpleNamespace(command='recover',
                                       expected_revision=pending['policy_revision']))
        policy = registry.load_policy()
        assert policy['policy_revision'] == 2
        assert policy['canonical_origin'] == 'https://new.example.test'
        print(json.dumps({'status': 'passed', 'origin_recovered': True}))
        raise SystemExit(0)
    if case == 'registry_origin_recover_cutover':
        pending = registry._pending_state()
        if pending is not None:
            admin._run(SimpleNamespace(command='recover',
                                       expected_revision=pending['policy_revision']))
        if registry._read_fixed(registry.SELECTOR_PATH, 4096, required=False) is None:
            admin._run(SimpleNamespace(command='cutover', expected_revision=3))
        assert registry.selector_mode()[0] == 'registry'
        print(json.dumps({'status': 'passed', 'cutover_recovered': True}))
        raise SystemExit(0)
    raise RuntimeError('unknown registry-origin process scenario')

from fwrouter_api.core.config import Settings, get_settings
Settings.model_config['env_file'] = None
get_settings.cache_clear()
from fwrouter_api.services import maintenance_lifecycle_admission as admission
admission._trusted_uid = os.getuid

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
