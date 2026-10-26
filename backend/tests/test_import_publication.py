"""Disposable SQLite/file boundaries; no native runtime or host qualification."""
from __future__ import annotations

import copy

import pytest
from _test_support import configure_test_state_dir as _env, maintenance_import_test_client
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services import control_plane_import_files as files
from fwrouter_api.services import control_plane_transfer_import as owner
from fwrouter_api.services.control_plane_transfer_export import export_control_plane_snapshot
from fwrouter_api.services.rules import get_manual_rules_texts, save_manual_draft
from fwrouter_api.services.rules_state_store import get_rules_state, _upsert_rules_state_record

pytestmark = pytest.mark.usefixtures("isolated_host_observations")


def _fixture(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    initialize_database()
    save_manual_draft('DOMAIN,old.example,DIRECT\n')
    snapshot = export_control_plane_snapshot(write_file=False)['snapshot']
    snapshot['state']['rules']['content']['manual_draft_text'] = 'DOMAIN,new.example,VPN\n'
    return snapshot, get_manual_rules_texts()


def _post(client, snapshot, operation_id='1' * 32):
    return client.post('/api/v2/transfer/control-plane/import', json={
        'snapshot': snapshot, 'operation_id': operation_id,
    })


def _revision():
    with db_session() as connection:
        row = connection.execute("SELECT value_json FROM settings WHERE key='core.intent_revision'").fetchone()
    return row[0] if row else None


@pytest.mark.parametrize('boundary', range(1, 12))
def test_preparation_failure_never_changes_active_set(monkeypatch, tmp_path, boundary):
    snapshot, old = _fixture(monkeypatch, tmp_path)
    paths = get_rules_state()
    revision = _revision()
    fsync = files.os.fsync
    calls = 0

    def fail(descriptor):
        nonlocal calls
        calls += 1
        if calls == boundary:
            raise OSError('test-owned I/O failure')
        fsync(descriptor)

    with maintenance_import_test_client(monkeypatch, tmp_path, raise_server_exceptions=False) as client:
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'fsync', fail)
            response = _post(client, snapshot)
        assert response.status_code == 500
        assert get_rules_state() == paths
        assert get_manual_rules_texts()['draft_text'] == old['draft_text']
        assert _revision() == revision
        # A retry uses an entirely prepared new set and the same SQL commit.
        result = _post(client, snapshot).json()
        assert result['ok'] is True
        assert get_manual_rules_texts()['draft_text'] == 'DOMAIN,new.example,VPN\n'


def test_retry_receipt_survives_restart_and_later_import(monkeypatch, tmp_path):
    snapshot, old = _fixture(monkeypatch, tmp_path)
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        first = _post(client, snapshot).json()['data']['import']
        revision = _revision()
        assert first['replayed'] is False
        assert old['draft_path'].read_text() == old['draft_text']
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        retry = _post(client, snapshot).json()['data']['import']
        assert retry['replayed'] is True
        assert retry['rules_files'] == first['rules_files']
        assert _revision() == revision
        altered = copy.deepcopy(snapshot)
        altered['state']['rules']['content']['manual_draft_text'] = 'DOMAIN,later.example,DIRECT\n'
        assert _post(client, altered, '2' * 32).json()['ok']
        newer_revision = _revision()
        assert _post(client, snapshot).json()['data']['import']['replayed'] is True
        assert _revision() == newer_revision
        assert get_manual_rules_texts()['draft_text'] == 'DOMAIN,later.example,DIRECT\n'
        conflict = _post(client, altered).json()
        assert conflict['ok'] is False
        assert conflict['data']['import']['error_code'] == 'IMPORT_OPERATION_ID_CONFLICT'
    exported = export_control_plane_snapshot(write_file=False)['snapshot']
    assert not any(r['key'].startswith(files.RECEIPT_PREFIX) for r in exported['state']['settings'])


def test_stale_path_writer_cannot_rebind_imported_set(monkeypatch, tmp_path):
    snapshot, _ = _fixture(monkeypatch, tmp_path)
    stale = get_rules_state()
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        assert _post(client, snapshot).json()['ok']
    active = get_rules_state()
    with pytest.raises(ValueError, match='RULES_PUBLICATION_STALE'):
        _upsert_rules_state_record({**stale, 'status': 'success'})
    assert get_rules_state() == active
    save_manual_draft('DOMAIN,edited.example,DIRECT\n')
    assert get_manual_rules_texts()['draft_text'] == 'DOMAIN,edited.example,DIRECT\n'
    assert get_rules_state()['manual_draft_path'] == active['manual_draft_path']


def test_post_commit_projection_failure_does_not_repeat_mutation(monkeypatch, tmp_path):
    snapshot, _ = _fixture(monkeypatch, tmp_path)
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(owner, 'build_system_summary', lambda: (_ for _ in ()).throw(OSError('test projection')))
        first = _post(client, snapshot).json()['data']['import']
        assert first['persistence_status'] == 'persisted'
        assert first['post_import_status'] == 'unavailable'
        revision = _revision()
        assert _post(client, snapshot).json()['data']['import']['replayed']
        assert _revision() == revision



def test_concurrent_retry_has_one_commit(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    snapshot, _ = _fixture(monkeypatch, tmp_path)
    ready = Barrier(2)
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        def invoke():
            ready.wait(timeout=5)
            return _post(client, snapshot).json()['data']['import']
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: invoke(), range(2)))
    assert sorted(r['replayed'] for r in responses) == [False, True]
    assert responses[0]['rules_files'] == responses[1]['rules_files']
    with db_session() as connection:
        assert connection.execute('SELECT count(*) FROM settings WHERE key GLOB ?',
                                  (files.RECEIPT_PREFIX + '*',)).fetchone()[0] == 1


def test_foreign_generation_and_receipt_are_not_authority(monkeypatch, tmp_path):
    from fwrouter_api.services.subscription import get_subscription_native_generation_state

    snapshot, _ = _fixture(monkeypatch, tmp_path)
    snapshot['state']['settings'].extend([
        {'key': 'subscriptions.native_generation.v1', 'value': {'status': 'verified', 'candidate_revision': 999}},
        {'key': files.RECEIPT_PREFIX + 'f' * 32, 'value': {'fingerprint': 'forged', 'result': {'ok': True}}},
    ])
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        assert _post(client, snapshot).json()['ok']
        generation = get_subscription_native_generation_state()
        assert generation['status'] != 'verified'
        assert generation['candidate_revision'] != 999
        with db_session() as connection:
            assert connection.execute('SELECT 1 FROM settings WHERE key=?',
                                      (files.RECEIPT_PREFIX + 'f' * 32,)).fetchone() is None


def test_sql_failure_preserves_previous_snapshot_and_no_receipt(monkeypatch, tmp_path):
    snapshot, old = _fixture(monkeypatch, tmp_path)
    state, revision = get_rules_state(), _revision()
    with maintenance_import_test_client(monkeypatch, tmp_path, raise_server_exceptions=False) as client:
        original = owner._insert_rows
        calls = 0

        def fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('test transaction failure after prepared files')
            return original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(owner, '_insert_rows', fail)
            assert _post(client, snapshot).status_code == 500
        assert get_rules_state() == state
        assert _revision() == revision
        assert get_manual_rules_texts()['draft_text'] == old['draft_text']
        with db_session() as connection:
            assert connection.execute('SELECT 1 FROM settings WHERE key GLOB ?',
                                      (files.RECEIPT_PREFIX + '*',)).fetchone() is None
        assert _post(client, snapshot).json()['ok']


def test_symlink_release_parent_is_denied_before_publication(monkeypatch, tmp_path):
    from fwrouter_api.core.config import get_settings

    snapshot, _ = _fixture(monkeypatch, tmp_path)
    state = get_rules_state()
    outside = tmp_path / 'unowned-destination'
    outside.mkdir()
    (get_settings().paths.state_dir / 'import-rules').symlink_to(outside, target_is_directory=True)
    with maintenance_import_test_client(monkeypatch, tmp_path, raise_server_exceptions=False) as client:
        assert _post(client, snapshot).status_code == 500
    assert get_rules_state() == state
    assert list(outside.iterdir()) == []


def test_publication_commit_uses_full_sqlite_durability(monkeypatch, tmp_path):
    snapshot, _ = _fixture(monkeypatch, tmp_path)
    original = owner._import_in_transaction
    observed = []

    def inspect(*args, **kwargs):
        connection = kwargs['connection']
        observed.append(connection.execute('PRAGMA synchronous').fetchone()[0])
        assert connection.in_transaction
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, '_import_in_transaction', inspect)
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        assert _post(client, snapshot).json()['ok']
    assert observed == [2]


def test_active_job_rejects_import_before_file_preparation(monkeypatch, tmp_path):
    snapshot, old = _fixture(monkeypatch, tmp_path)
    revision, paths = _revision(), get_rules_state()
    with db_session() as connection:
        connection.execute("INSERT INTO jobs(job_id,job_type,status) VALUES ('owned-job','rules_apply','running')")

    def forbidden(*args, **kwargs):
        pytest.fail('rejected import must not prepare publication files')

    monkeypatch.setattr(owner, '_write_rules_files_from_snapshot', forbidden)
    with maintenance_import_test_client(monkeypatch, tmp_path) as client:
        response = _post(client, snapshot).json()
    assert response['ok'] is False
    assert response['data']['import']['error_code'] == 'IMPORT_ACTIVE_JOB_CONFLICT'
    assert get_rules_state() == paths
    assert get_manual_rules_texts()['draft_text'] == old['draft_text']
    assert _revision() == revision
    with db_session() as connection:
        assert connection.execute("SELECT status FROM jobs WHERE job_id='owned-job'").fetchone()[0] == 'running'
        assert connection.execute('SELECT count(*) FROM settings WHERE key GLOB ?',
                                  (files.RECEIPT_PREFIX + '*',)).fetchone()[0] == 0
