"""Disposable SQLite/flock tests; physical runtime observations are synthetic."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import rollout_native_evidence as rollout
from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint


@pytest.fixture
def operation(monkeypatch):
    root = get_settings().paths.state_dir
    directory = root / 'state' / 'controlled-rollout'
    directory.mkdir(parents=True, mode=0o700)
    for path in (root, root / 'state', directory):
        path.chmod(0o700)
    lock = directory / '.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lease_fd = rollout.admission._open_lease(root, create=True)
    fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    oid, old, new = str(uuid4()), 'a' * 64, 'b' * 64
    runtime = dict(kind='mihomo_existing_container/v1', phase='released', operation_id=oid,
                   source_manifest_sha256=old, unit_active_state='active', unit_sub_state='exited',
                   unit_enabled_state='enabled', container_id='c' * 64, image_id='sha256:' + 'd' * 64,
                   config_image='metacubex/mihomo:v1.19.31', compose_config_hash='e' * 64,
                   compose_file_sha256='f' * 64,
                   cache_before={'sha256': '0' * 64, 'size': 1},
                   cache_after={'sha256': '1' * 64, 'size': 2})
    checkpoint = dict(schema=4, status='pending', operation_id=oid, backend_manifest_sha256=new,
                      maintenance_operation=None, runtime_quiescence=runtime,
                      initial_backend_manifest_sha256=old, source_transitions=[dict(
                          operation_id=oid, from_manifest_sha256=old, to_manifest_sha256=new)])
    path = directory / 'pending.json'
    path.write_text(json.dumps(checkpoint))
    path.chmod(0o600)
    monkeypatch.setattr(rollout.admission, '_require_units_stopped', lambda: None)
    monkeypatch.setattr(rollout.native_proof, 'backend_manifest_sha256', lambda root: new)
    with db_session() as db:
        db.execute("INSERT INTO servers(server_id,server_name,provider_name,raw_json,inventory_state) "
                   "VALUES('fixture','fixture','subscription','{}','active')")
        digest, count = rollout.subscription._subscription_inventory_digest(db)
        marker = dict(schema=1, status='verified', candidate_digest=digest, candidate_revision=3,
                      verified_digest=digest, verified_revision=3, reason=None, inventory_count=count,
                      native_proof={'old': True})
        db.execute('INSERT INTO settings(key,value_json) VALUES(?,?)',
                   (rollout.subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker)))
    receipt = root / 'generated' / 'mihomo' / 'reconcile-state.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text('{}')
    monkeypatch.setattr(fingerprint, '_state_path', lambda: receipt)
    proof = dict(ok=True, source_fingerprint='source', core_runtime_fingerprint='core',
                 selection_revision=1, selection_decision_id=None, selection_snapshot={}, routing_snapshot={},
                 xray_runtime_incarnation='xray', mihomo_runtime_incarnation='mihomo',
                 xray_config_sha256='1' * 64, mihomo_config_sha256='2' * 64,
                 mihomo_fingerprint_hash='3' * 64, core_dataplane_proof=dict(
                     manifest_hash='4' * 64, apply_id='apply', enforcement_level='full', live_global_mode='auto'))
    monkeypatch.setattr(rollout.owner, '_legacy_admission_source_fingerprint', lambda: 'source')
    monkeypatch.setattr(rollout.owner, '_xray_adapter', lambda: None)
    monkeypatch.setattr(rollout.owner, '_xray_generation_checkpoint_path', lambda adapter: root / 'xray-pending')
    monkeypatch.setattr(rollout.owner, '_collect_legacy_inventory_native_proof', lambda *args, **kw: proof)
    monkeypatch.setattr(rollout.owner, '_legacy_admission_runtime_fences_match', lambda fence: True)
    calls = []

    def publish(**kw):
        calls.append('publish')
        assert kw['reconcile_reason'] == 'controlled_rollout_native_evidence'
        assert kw['owner_matches'](proof['mihomo_fingerprint_hash'])
        receipt.write_text('{"verified":true}')
        return True

    # Keep real capability issuance + SQLite marker validation; substitute
    # physical proof/promotion here. Existing owner suite covers native CAS.
    def promote(capability):
        calls.append('promote')
        assert capability.issuer is rollout.subscription._SUBSCRIPTION_ADMISSION_ISSUER
        assert capability.requires_native_proof_refresh
        assert capability.proof_only_source_fingerprint == 'source'
        with db_session() as db:
            latest = json.loads(db.execute('SELECT value_json FROM settings WHERE key=?',
                (rollout.subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,)).fetchone()[0])
            latest['native_proof'] = {'fresh': True}
            db.execute('UPDATE settings SET value_json=? WHERE key=?',
                       (json.dumps(latest), rollout.subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY))
        return {'ok': True}

    monkeypatch.setattr(rollout.owner, '_publish_mihomo_udp_receipt_with_owner', publish)
    monkeypatch.setattr(rollout.subscription, '_promote_subscription_inventory_generation', promote)
    args = dict(expected_operation_id=oid, expected_backend_manifest_sha256=new,
                expected_generation_digest=digest, expected_generation_revision=3,
                expected_checkpoint_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), lifecycle_lock_fd=fd, maintenance_lease_fd=lease_fd)
    yield SimpleNamespace(args=args, checkpoint=checkpoint, path=path, calls=calls, marker=marker,
                          receipt=receipt, proof=proof, root=root)
    os.close(lease_fd)
    os.close(fd)


def invoke(operation):
    return rollout.refresh_rollout_native_evidence(**operation.args)


def test_refresh_preserves_verified_tuple_and_reports_only_owned_transition(operation):
    result = invoke(operation)
    assert result['ok'] is True
    assert result['runtime_apply_performed'] is False
    assert result['generation_revision'] == 3
    assert result['owner_artifact_transition']['relative_path'] == 'generated/mihomo/reconcile-state.json'
    assert operation.calls == ['publish', 'promote']
    after = rollout._verified_marker(operation.args['expected_generation_digest'], 3)
    assert {k: v for k, v in after.items() if k != 'native_proof'} == {
        k: v for k, v in operation.marker.items() if k != 'native_proof'}


@pytest.mark.parametrize('field,value', [
    ('expected_operation_id', str(uuid4())), ('expected_checkpoint_sha256', '0' * 64),
    ('expected_backend_manifest_sha256', '0' * 64), ('expected_generation_digest', '0' * 64),
    ('expected_generation_revision', 4), ('lifecycle_lock_fd', -1), ('maintenance_lease_fd', -1),
])
def test_wrong_identity_rejected_before_publication(operation, field, value):
    operation.args[field] = value
    assert invoke(operation)['ok'] is False
    assert operation.calls == []


@pytest.mark.parametrize('mutation', ['pending', 'foreign_operation', 'wrong_source', 'unknown_marker',
                                      'inventory_changed', 'unit_active', 'native_incomplete', 'nonroot'])
def test_preflight_fences_block_before_writes(operation, monkeypatch, mutation):
    if mutation in {'pending', 'foreign_operation', 'wrong_source'}:
        if mutation == 'pending':
            operation.checkpoint['runtime_quiescence']['phase'] = 'release_pending'
        elif mutation == 'foreign_operation':
            operation.checkpoint['source_transitions'][0]['operation_id'] = str(uuid4())
        else:
            operation.checkpoint['source_transitions'][0]['to_manifest_sha256'] = '0' * 64
        operation.path.write_text(json.dumps(operation.checkpoint))
        operation.args['expected_checkpoint_sha256'] = hashlib.sha256(operation.path.read_bytes()).hexdigest()
    elif mutation == 'unknown_marker':
        with db_session() as db:
            marker = dict(operation.marker, future_sensitive_extension=True)
            db.execute('UPDATE settings SET value_json=? WHERE key=?', (json.dumps(marker),
                rollout.subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY))
    elif mutation == 'inventory_changed':
        with db_session() as db:
            db.execute("UPDATE servers SET server_name='changed'")
    elif mutation == 'unit_active':
        def active():
            raise rollout.admission.MaintenanceAdmissionError('unit active')
        monkeypatch.setattr(rollout.admission, '_require_units_stopped', active)
    elif mutation == 'native_incomplete':
        operation.proof['ok'] = False
    else:
        monkeypatch.setattr(rollout.os, 'geteuid', lambda: 1000)
    assert invoke(operation)['ok'] is False
    assert operation.calls == []


def test_checkpoint_changed_during_native_observation_blocks(operation, monkeypatch):
    def collect(*args, **kw):
        operation.path.write_text('{}')
        return operation.proof
    monkeypatch.setattr(rollout.owner, '_collect_legacy_inventory_native_proof', collect)
    assert invoke(operation)['ok'] is False
    assert operation.calls == []


def test_receipt_publication_failure_is_not_success(operation, monkeypatch):
    monkeypatch.setattr(rollout.owner, '_publish_mihomo_udp_receipt_with_owner', lambda **kw: False)
    assert invoke(operation)['ok'] is False
    assert operation.calls == []


def test_generation_commit_failure_is_not_success(operation, monkeypatch):
    monkeypatch.setattr(rollout.subscription, '_promote_subscription_inventory_generation', lambda cap: {'ok': False})
    assert invoke(operation)['ok'] is False
    assert operation.calls == ['publish']


def test_unresolved_generation_checkpoint_rejected(operation, monkeypatch):
    monkeypatch.setattr(rollout.owner, 'subscription_inventory_checkpoint_write_gate',
                        lambda: {'ok': False})
    assert invoke(operation)['ok'] is False
    assert operation.calls == []


def test_checkpoint_change_at_receipt_owner_boundary_rejected(operation, monkeypatch):
    def publish(**kw):
        operation.path.write_text('{}')
        assert kw['owner_matches'](operation.proof['mihomo_fingerprint_hash']) is not True
        return False
    monkeypatch.setattr(rollout.owner, '_publish_mihomo_udp_receipt_with_owner', publish)
    assert invoke(operation)['ok'] is False
    assert operation.calls == []


def test_unapproved_receipt_path_rejected(operation, monkeypatch):
    monkeypatch.setattr(fingerprint, '_state_path', lambda: operation.root / 'another.json')
    assert invoke(operation)['ok'] is False
    assert operation.calls == []


def test_cli_dispatches_exact_owned_request_and_fails_closed(operation, monkeypatch, capsys):
    from fwrouter_api import cli
    from contextlib import nullcontext
    monkeypatch.setattr(cli, 'runtime_shared_lease', lambda root: nullcontext(), raising=False)
    # main imports the lease authority locally; substitute only disposable
    # shared lease plumbing. The owned descriptor is validated by the service.
    monkeypatch.setattr(rollout.admission, 'runtime_shared_lease', lambda root: nullcontext())
    seen = []
    def refresh(**kw):
        seen.append(kw)
        return {'ok': False, 'reason': 'synthetic_unverified'}
    monkeypatch.setattr(rollout, 'refresh_rollout_native_evidence', refresh)
    arguments = ['xray', 'generation', 'proof-only-refresh']
    for key, value in operation.args.items():
        arguments.extend(['--' + key.replace('_', '-'), str(value)])
    assert cli.main(arguments) == 1
    assert seen == [operation.args]
    assert json.loads(capsys.readouterr().out) == {'ok': False, 'reason': 'synthetic_unverified'}


@pytest.mark.parametrize('transition', [None, [], 'invalid', 1])
def test_malformed_transition_is_safe_error(operation, transition):
    operation.checkpoint['source_transitions'] = [transition]
    operation.path.write_text(json.dumps(operation.checkpoint))
    operation.args['expected_checkpoint_sha256'] = hashlib.sha256(operation.path.read_bytes()).hexdigest()
    assert invoke(operation) == {'ok': False, 'status': 'blocked',
                                 'reason': 'rollout_native_evidence_unverified'}
    assert operation.calls == []


def test_sqlite_failure_is_safe_cli_error(operation, monkeypatch, capsys):
    import sqlite3
    from fwrouter_api import cli
    def fail(*args, **kw):
        raise sqlite3.OperationalError('sensitive-path-and-query')
    monkeypatch.setattr(rollout, '_verified_marker', fail)
    arguments = ['xray', 'generation', 'proof-only-refresh']
    for key, value in operation.args.items():
        arguments.extend(['--' + key.replace('_', '-'), str(value)])
    assert cli.main(arguments) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['reason'] == 'rollout_native_evidence_unverified'
    assert 'sensitive' not in output.out + output.err
    assert 'Traceback' not in output.out + output.err


def test_api_start_admission_cannot_acquire_shared_lease_during_owner_commit(operation, monkeypatch):
    actual_flock = fcntl.flock
    observations = []
    def nonblocking_test_flock(fd, flags):
        # Exercise real admission and kernel exclusion without hanging a test
        # waiting for an owner that this same thread deliberately still holds.
        return actual_flock(fd, flags | fcntl.LOCK_NB)
    monkeypatch.setattr(rollout.admission.fcntl, 'flock', nonblocking_test_flock)
    original_collect = rollout.owner._collect_legacy_inventory_native_proof
    def collect(*args, **kw):
        with pytest.raises(BlockingIOError):
            rollout.admission.acquire_shared_lease_fd(operation.root)
        observations.append('startup_excluded')
        return original_collect(*args, **kw)
    monkeypatch.setattr(rollout.owner, '_collect_legacy_inventory_native_proof', collect)
    assert invoke(operation)['ok'] is True
    assert observations == ['startup_excluded']
