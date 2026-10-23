"""Public data-contract tests; never import or execute the private root wrapper."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

CONTRACT = Path(__file__).parent / 'fixtures/manual-deployment-contract.json'
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


def validate_public_receipt(value: dict) -> None:
    """Validate an illustrative redacted summary, not CI or deployment authority."""
    allowed = {'schema', 'source_sha', 'scope', 'status', 'gates', 'private_qualification'}
    if set(value) != allowed:
        raise ValueError('unexpected receipt fields; private data must not be published')
    if value['schema'] != 'fwrouter-manual-deployment-public-summary/v1':
        raise ValueError('schema')
    sha = value['source_sha']
    if not isinstance(sha, str) or len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('sha')
    if value['scope'] != 'ui':
        raise ValueError('unsupported scope')
    gates = value['gates']
    required_gates = {
        'local_source', 'remote_main', 'ci_v3_main_evidence', 'preflight', 'native_proof',
        'ui_integrity', 'installer', 'postflight',
    }
    if not isinstance(gates, dict) or set(gates) != required_gates:
        raise ValueError('gate completeness')
    if any(status not in {'PASS', 'FAIL', 'NOT RUN', 'UNKNOWN', 'INCOMPLETE', 'STALE'}
           for status in gates.values()):
        raise ValueError('gate status')
    if value['status'] not in {'PASS', 'FAIL', 'CHECK ONLY', 'UNKNOWN'}:
        raise ValueError('overall status')
    if value['status'] == 'PASS' and any(status != 'PASS' for status in gates.values()):
        raise ValueError('partial success is not deployment success')
    if value['private_qualification'] != 'separate-private-hash-bound-evidence':
        raise ValueError('public CI must not claim private wrapper qualification')


def sample() -> dict:
    return {'schema': 'fwrouter-manual-deployment-public-summary/v1', 'source_sha': 'a' * 40,
            'scope': 'ui', 'status': 'PASS',
            'gates': dict.fromkeys([
                'local_source', 'remote_main', 'ci_v3_main_evidence', 'preflight', 'native_proof',
                'ui_integrity', 'installer', 'postflight',
            ], 'PASS'),
            'private_qualification': 'separate-private-hash-bound-evidence'}


def validate_main_ci_provenance(run: dict, jobs: list[dict], artifact: dict, *,
                                expected_sha: str, expected_attempt: int,
                                now: datetime) -> None:
    """Validate a normalized fresh GitHub API snapshot, not deployment authority."""
    if (run.get('repository') != 'Coxf0rd/fwrouter'
            or run.get('workflow') != 'FWRouter affected CI'
            or run.get('workflow_path') != '.github/workflows/test-gates.yml'
            or run.get('event') != 'push' or run.get('ref') != 'refs/heads/main'
            or run.get('head_branch') != 'main' or run.get('status') != 'completed'
            or run.get('conclusion') != 'success' or run.get('head_sha') != expected_sha
            or type(run.get('run_id')) is not int or run['run_id'] <= 0
            or type(run.get('run_attempt')) is not int or run['run_attempt'] != expected_attempt):
        raise ValueError('main workflow identity or result')
    expected_names = {'Immutable plan and preflight', 'Build and verify source once'}
    expected_names.update(
        f'Routine and contract shard ({group})' for group in (
            'core-state', 'routing-adapters', 'services-events', 'ui-tooling',
            'api-persistence-contracts', 'ui-provider-contracts',
            'local-integration', 'installer-startup',
        )
    )
    expected_names.update({
        'Ordinary functional and browser', 'Provider and browser cohort',
        'Kernel packet and fail-closed', 'Qualified native profile (qcp)',
        'Qualified native profile (xray)', 'Affected L0-L5 with hosted profiles',
    })
    names = [job.get('name') for job in jobs]
    if len(jobs) != 16 or set(names) != expected_names:
        raise ValueError('required or unique runner jobs')
    if any(job.get('run_id') != run['run_id']
           or job.get('run_attempt') != expected_attempt
           or job.get('status') != 'completed'
           or job.get('conclusion') != 'success' for job in jobs):
        raise ValueError('job run/attempt eligibility')
    required = next(job for job in jobs if job['name'] == 'Affected L0-L5 with hosted profiles')
    if required.get('required_check') != 'success' or required.get('main_smoke') != 'passed':
        raise ValueError('strict aggregate or main smoke')
    name = f"fwrouter-main-evidence-{expected_sha}-{run['run_id']}-{expected_attempt}"
    try:
        expires_at = datetime.fromisoformat(artifact['expires_at'].replace('Z', '+00:00'))
        if expires_at.tzinfo is None or now.tzinfo is None:
            raise ValueError
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ValueError('artifact expiry') from None
    digest = artifact.get('digest')
    digest = digest.removeprefix('sha256:') if isinstance(digest, str) else ''
    if (type(artifact.get('artifact_id')) is not int or artifact['artifact_id'] <= 0
            or artifact.get('name') != name
            or type(artifact.get('size_in_bytes')) is not int
            or not 0 < artifact['size_in_bytes'] <= MAX_ARTIFACT_BYTES
            or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest)
            or artifact.get('expired') is not False or expires_at <= now
            or artifact.get('workflow_run_id') != run['run_id']
            or artifact.get('workflow_run_head_sha') != expected_sha):
        raise ValueError('main artifact provenance, identity, digest, or expiry')


REQUIRED_NATIVE_CHECKS = (
    'backend_manifest_sha256', 'sqlite_schema_integrity_foreign_keys',
    'generation_owner_state', 'checkpoint_state',
    'xray_identity_bindings_config_runtime', 'mihomo_config_rules_selectors_runtime',
    'core_manifest_nft_ip_routing_runtime', 'final_native_runtime_fence_recheck',
    'generation_native_receipt_exact_match',
)


def validate_native_proof(value: dict, *, backend_sha: str, xray_image: str, mihomo_image: str) -> None:
    """Check the public CLI contract; this synthetic validator grants no deployment authority."""
    required = {
        'proof_schema_version', 'checks_executed', 'scope', 'source_identity', 'database',
        'xray', 'mihomo', 'core', 'status', 'reason', 'native_proof_sha256',
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('native proof shape')
    if type(value['proof_schema_version']) is not int or value['proof_schema_version'] != 1 or value['scope'] != 'ui':
        raise ValueError('native proof version or scope')
    if value['status'] != 'PASS' or value['reason'] is not None:
        raise ValueError('native proof is not PASS')
    source = value['source_identity']
    if source != {'kind': 'backend_manifest_sha256', 'sha256': backend_sha, 'commit_sha': None}:
        raise ValueError('backend identity')
    checks = value['checks_executed']
    if checks != list(REQUIRED_NATIVE_CHECKS):
        raise ValueError('required native checks')
    database = value['database']
    if not isinstance(database, dict) or set(database) != {
        'schema', 'integrity_check', 'foreign_key_violations', 'generation',
        'selection_revision', 'core_intent_revision', 'latest_apply_status',
        'active_job_count', 'checkpoint',
    }:
        raise ValueError('database facts')
    schema = database.get('schema')
    generation = database.get('generation')
    checkpoint = database.get('checkpoint')
    if (not isinstance(schema, dict) or set(schema) != {'ok', 'expected_version', 'actual_version'}
            or not isinstance(generation, dict)
            or set(generation) != {
                'status', 'candidate_revision', 'verified_revision', 'inventory_count',
                'native_proof_present', 'native_proof_sha256',
            }
            or not isinstance(checkpoint, dict) or set(checkpoint) != {'present', 'valid'}):
        raise ValueError('database nested schema')
    candidate_revision = generation.get('candidate_revision')
    verified_revision = generation.get('verified_revision')
    inventory_count = generation.get('inventory_count')
    receipt_sha = generation.get('native_proof_sha256')
    proof_sha = value['native_proof_sha256']
    if (schema.get('ok') is not True
            or type(schema.get('expected_version')) is not int
            or schema.get('expected_version') < 1
            or type(schema.get('actual_version')) is not int
            or schema.get('actual_version') != schema.get('expected_version')
            or database.get('integrity_check') != ['ok']
            or type(database.get('foreign_key_violations')) is not int
            or database.get('foreign_key_violations') != 0
            or generation.get('status') != 'verified'
            or generation.get('native_proof_present') is not True
            or type(candidate_revision) is not int or candidate_revision < 0
            or type(verified_revision) is not int or verified_revision != candidate_revision
            or type(inventory_count) is not int or inventory_count < 0
            or not _valid_sha256(receipt_sha)
            or not _valid_sha256(proof_sha) or receipt_sha != proof_sha
            or type(database.get('selection_revision')) is not int
            or database.get('selection_revision') < 0
            or type(database.get('core_intent_revision')) is not int
            or database.get('core_intent_revision') < 0
            or database.get('latest_apply_status') != 'applied'
            or type(database.get('active_job_count')) is not int
            or database.get('active_job_count') != 0
            or checkpoint.get('present') is not False
            or checkpoint.get('valid') is not True):
        raise ValueError('database or owner state')
    if not isinstance(value['xray'], dict) or value['xray'].get('runtime_image_digest') != xray_image:
        raise ValueError('Xray runtime identity')
    if not isinstance(value['mihomo'], dict) or value['mihomo'].get('runtime_image_digest') != mihomo_image:
        raise ValueError('Mihomo runtime identity')
    if (not isinstance(value['core'], dict)
            or value['core'].get('native_rule_semantics_match_owner_receipt') is not True
            or value['core'].get('ip_rules_stable') is not True
            or value['core'].get('ip_routes_stable') is not True):
        raise ValueError('Core native owner evidence')
def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in '0123456789abcdef' for char in value
    )


def test_public_contract_preserves_owner_control_and_local_source():
    contract = json.loads(CONTRACT.read_text())
    assert contract['schema'] == 'fwrouter-manual-deployment-contract/v3'
    assert contract['execution'] == 'owner-invoked-local-only'
    assert contract['wrapper_distribution'] == 'outside-repository-owner-controlled'
    assert contract['source'] == 'verified-existing-local-git-objects'
    assert contract['remote_purpose'] == 'metadata-and-ci-evidence-only'
    assert contract['automatic_deployment'] is False
    assert contract['initial_scopes'] == ['ui']
    assert {'backend', 'schema', 'recovery', 'host'} <= set(contract['unsupported_scopes'])


def test_contract_uses_installer_owned_atomic_ui_publication():
    contract = json.loads(CONTRACT.read_text())
    ui = contract['ui_publication']
    assert ui['installer_operation'] == 'installer/install.sh --deploy --component ui'
    assert ui['publisher'] == 'installer/ui_publication.py'
    assert ui['atomic_switch'] == 'renameat2(RENAME_EXCHANGE)'
    assert ui['wrapper_may_mutate_pointer'] is False
    assert ui['native_proof_attests_ui_pointer'] is False
    assert ui['automatic_rollback'] is False
    assert 'browser-session-across-requests' in ui['not_guaranteed']
    assert set(ui['deploy_side_effects']) == {
        'bootstrap-state-directories-and-markers',
        'state-database-and-backup-permissions', 'api-env-mode-0600',
    }


def test_contract_blocks_legacy_backend_without_native_owner_receipts():
    contract = json.loads(CONTRACT.read_text())
    assert 'UI-only deployment cannot install this prerequisite' in contract['legacy_backend_prerequisite']
    proof = contract['native_proof']
    assert proof['command'] == 'fwrouter native-proof --scope ui'
    assert proof['accepted_status'] == 'PASS'
    assert set(proof['rejected_statuses']) == {'FAIL', 'STALE', 'INCOMPLETE'}
    assert proof['side_effects'] == []
    assert 'no synthetic receipt' in proof['missing_owner_receipt']


def test_ci_v3_contract_requires_fresh_api_metadata_and_complete_main_bundle():
    contract = json.loads(CONTRACT.read_text())
    evidence = contract['ci_evidence']
    assert evidence['source_of_truth'] == 'fresh-read-only-GitHub-API-metadata'
    assert evidence['actual_runner_job_count'] == 16
    assert len(evidence['expected_runner_job_names']) == 16
    assert evidence['job_inventory_policy'] == 'exact-names-no-missing-duplicate-extra-or-skipped-jobs'
    assert evidence['required_check'] == 'Affected L0-L5 with hosted profiles'
    assert evidence['artifact_retention_days'] == 7
    assert type(evidence['artifact_max_size_bytes']) is int
    assert evidence['artifact_max_size_bytes'] == MAX_ARTIFACT_BYTES
    assert evidence['current_attempt_jobs_only'] is True
    assert evidence['main_smoke_required_and_passed'] is True
    assert evidence['execute_code_from_bundle'] is False
    assert evidence['cached_bundle_fallback'] is False
    assert evidence['legacy_ci_v2_artifact_fallback'] is False
    assert 'workflow_run_attempt' not in evidence['artifact_metadata_fields']
    assert 'attempt-specific jobs API' in evidence['attempt_binding']
    assert {'routine-shard-node-inventory', 'native-mihomo-xray-proofs',
            'kernel-packet-receipts', 'safe-recovery-receipts'} <= set(evidence['bundle_must_retain'])
    assert evidence['offline_verifier'].startswith('tests/gates/evidence_bundle.py verify --require-main')
    assert contract['l6_policy']['deployment_authority'] is False
    assert contract['l6_policy']['required_by_initial_ui_scope'] is False


def valid_main_api_snapshot():
    sha = 'a' * 40
    run = {
        'repository': 'Coxf0rd/fwrouter', 'workflow': 'FWRouter affected CI',
        'workflow_path': '.github/workflows/test-gates.yml', 'event': 'push',
        'ref': 'refs/heads/main', 'head_branch': 'main', 'status': 'completed',
        'conclusion': 'success', 'head_sha': sha, 'run_id': 8123456789,
        'run_attempt': 2,
    }
    names = ['Immutable plan and preflight', 'Build and verify source once']
    names += [f'Routine and contract shard ({group})' for group in (
        'core-state', 'routing-adapters', 'services-events', 'ui-tooling',
        'api-persistence-contracts', 'ui-provider-contracts',
        'local-integration', 'installer-startup',
    )]
    names += ['Ordinary functional and browser', 'Provider and browser cohort',
              'Kernel packet and fail-closed', 'Qualified native profile (qcp)',
              'Qualified native profile (xray)', 'Affected L0-L5 with hosted profiles']
    jobs = [{'name': name, 'run_id': run['run_id'], 'run_attempt': 2,
             'status': 'completed', 'conclusion': 'success'} for name in names]
    jobs[-1]['required_check'] = 'success'
    jobs[-1]['main_smoke'] = 'passed'
    assert set(names) == set(json.loads(CONTRACT.read_text())['ci_evidence']['expected_runner_job_names'])
    artifact = {
        'artifact_id': 54321,
        'name': f"fwrouter-main-evidence-{sha}-{run['run_id']}-2",
        'size_in_bytes': 8192, 'digest': 'b' * 64, 'expired': False,
        'expires_at': '2026-10-30T00:00:00Z',
        'workflow_run_id': run['run_id'], 'workflow_run_head_sha': sha,
    }
    return sha, run, jobs, artifact


def test_fresh_current_attempt_main_api_snapshot_is_eligible():
    sha, run, jobs, artifact = valid_main_api_snapshot()
    validate_main_ci_provenance(run, jobs, artifact, expected_sha=sha,
                                expected_attempt=2,
                                now=datetime(2026, 10, 23, tzinfo=timezone.utc))


@pytest.mark.parametrize('change', [
    'wrong_repo', 'wrong_workflow', 'wrong_event', 'wrong_ref', 'wrong_sha',
    'failed_run', 'wrong_attempt', 'missing_job', 'duplicate_job', 'failed_job',
    'wrong_job_attempt', 'missing_aggregate', 'aggregate_failed', 'smoke_missing',
    'wrong_artifact_name', 'wrong_artifact_run', 'wrong_artifact_sha', 'wrong_artifact_attempt',
    'expired', 'expired_time', 'missing_digest', 'partial_job_list',
])
def test_main_ci_provenance_fails_closed_on_identity_or_evidence_gap(change):
    sha, run, jobs, artifact = valid_main_api_snapshot()
    if change == 'wrong_repo': run['repository'] = 'attacker/fwrouter'
    elif change == 'wrong_workflow': run['workflow_path'] = '.github/workflows/other.yml'
    elif change == 'wrong_event': run['event'] = 'pull_request'
    elif change == 'wrong_ref': run['ref'] = 'refs/heads/feature'
    elif change == 'wrong_sha': run['head_sha'] = 'c' * 40
    elif change == 'failed_run': run['conclusion'] = 'failure'
    elif change == 'wrong_attempt': run['run_attempt'] = 1
    elif change == 'missing_job': jobs.pop()
    elif change == 'duplicate_job': jobs[-1]['name'] = jobs[0]['name']
    elif change == 'failed_job': jobs[4]['conclusion'] = 'skipped'
    elif change == 'wrong_job_attempt': jobs[4]['run_attempt'] = 1
    elif change == 'missing_aggregate': jobs = [job for job in jobs if not job['name'].startswith('Affected ')]
    elif change == 'aggregate_failed': jobs[-1]['required_check'] = 'failure'
    elif change == 'smoke_missing': jobs[-1]['main_smoke'] = 'not-run'
    elif change == 'wrong_artifact_name': artifact['name'] += '-old'
    elif change == 'wrong_artifact_run': artifact['workflow_run_id'] += 1
    elif change == 'wrong_artifact_sha': artifact['workflow_run_head_sha'] = 'c' * 40
    elif change == 'wrong_artifact_attempt': artifact['name'] += '-1'
    elif change == 'expired': artifact['expired'] = True
    elif change == 'expired_time': artifact['expires_at'] = '2026-10-22T00:00:00Z'
    elif change == 'missing_digest': artifact['digest'] = 'not-a-digest'
    elif change == 'partial_job_list': jobs = jobs[:8]
    with pytest.raises(ValueError):
        validate_main_ci_provenance(run, jobs, artifact, expected_sha=sha,
                                    expected_attempt=2,
                                    now=datetime(2026, 10, 23, tzinfo=timezone.utc))


def test_all_50_safety_scenarios_are_declared_without_production_execution():
    scenarios = json.loads(CONTRACT.read_text())['required_scenarios']
    assert [x['id'] for x in scenarios] == list(range(1, 51))
    assert len({x['name'] for x in scenarios}) == 50
    assert all(x['production_allowed'] is False for x in scenarios)


def test_log_retention_cannot_cover_permanent_receipts():
    contract = json.loads(CONTRACT.read_text())
    assert contract['temporary_log_days'] == 7
    assert contract['permanent_receipts_in_tmpfiles_scope'] is False


def test_complete_redacted_summary_is_valid_data_not_authority():
    validate_public_receipt(sample())


def test_old_ci_v2_gate_name_cannot_substitute_for_current_main_evidence():
    value = sample()
    value['gates']['ci_v2'] = value['gates'].pop('ci_v3_main_evidence')
    with pytest.raises(ValueError, match='gate completeness'):
        validate_public_receipt(value)


@pytest.mark.parametrize('gate', [
    'local_source', 'remote_main', 'ci_v3_main_evidence', 'preflight', 'native_proof',
    'ui_integrity', 'installer', 'postflight',
])
@pytest.mark.parametrize('status', ['FAIL', 'NOT RUN'])
def test_no_false_pass_for_incomplete_execution(gate, status):
    value = sample()
    value['gates'][gate] = status
    with pytest.raises(ValueError, match='partial success'):
        validate_public_receipt(value)


@pytest.mark.parametrize('field', ['token', 'credentials', 'sqlite', 'native_config', 'private_key', 'subscription_url'])
def test_private_fields_are_not_part_of_public_summary(field):
    value = sample()
    value[field] = 'SYNTHETIC_TEST_VALUE'
    with pytest.raises(ValueError, match='unexpected receipt fields'):
        validate_public_receipt(value)


@pytest.mark.parametrize('sha', ['main', 'abc1234', 'A' * 40, '../installer', 'a' * 39])
def test_exact_sha_is_data_not_shell_input(sha):
    value = sample()
    value['source_sha'] = sha
    with pytest.raises(ValueError, match='sha'):
        validate_public_receipt(value)


@pytest.mark.parametrize('scope', ['all', 'backend', 'schema', 'host', 'recovery'])
def test_summary_cannot_broaden_initial_scope(scope):
    value = sample()
    value['scope'] = scope
    with pytest.raises(ValueError, match='unsupported scope'):
        validate_public_receipt(value)


def test_public_ci_cannot_certify_unavailable_private_implementation():
    value = sample()
    value['private_qualification'] = 'tested-by-public-ci'
    with pytest.raises(ValueError, match='private wrapper qualification'):
        validate_public_receipt(value)


def native_sample() -> dict:
    return {
        'proof_schema_version': 1,
        'checks_executed': list(REQUIRED_NATIVE_CHECKS),
        'scope': 'ui',
        'source_identity': {'kind': 'backend_manifest_sha256', 'sha256': 'a' * 64, 'commit_sha': None},
        'database': {
            'schema': {'ok': True, 'expected_version': 27, 'actual_version': 27},
            'integrity_check': ['ok'], 'foreign_key_violations': 0,
            'generation': {
                'status': 'verified', 'candidate_revision': 7, 'verified_revision': 7,
                'inventory_count': 6, 'native_proof_present': True,
                'native_proof_sha256': 'd' * 64,
            },
            'selection_revision': 11, 'core_intent_revision': 9,
            'latest_apply_status': 'applied', 'active_job_count': 0,
            'checkpoint': {'present': False, 'valid': True},
        },
        'xray': {'runtime_image_digest': 'sha256:' + 'b' * 64},
        'mihomo': {'runtime_image_digest': 'sha256:' + 'c' * 64},
        'core': {
            'native_rule_semantics_match_owner_receipt': True,
            'ip_rules_stable': True, 'ip_routes_stable': True,
        },
        'status': 'PASS', 'reason': None, 'native_proof_sha256': 'd' * 64,
    }


def test_native_proof_contract_requires_exact_scope_pins_and_complete_owner_checks():
    validate_native_proof(
        native_sample(), backend_sha='a' * 64,
        xray_image='sha256:' + 'b' * 64, mihomo_image='sha256:' + 'c' * 64,
    )


@pytest.mark.parametrize('status', ['FAIL', 'STALE', 'INCOMPLETE'])
def test_native_proof_nonpass_status_never_satisfies_preflight(status):
    value = native_sample()
    value['status'] = status
    with pytest.raises(ValueError, match='not PASS'):
        validate_native_proof(
            value, backend_sha='a' * 64,
            xray_image='sha256:' + 'b' * 64, mihomo_image='sha256:' + 'c' * 64,
        )


def test_native_proof_missing_legacy_owner_receipt_is_rejected():
    value = native_sample()
    value['database']['generation']['native_proof_present'] = False
    with pytest.raises(ValueError, match='database (nested schema|or owner state)'):
        validate_native_proof(
            value, backend_sha='a' * 64,
            xray_image='sha256:' + 'b' * 64, mihomo_image='sha256:' + 'c' * 64,
        )


@pytest.mark.parametrize('mutation', [
    lambda generation: generation.update(verified_revision=8),
    lambda generation: generation.update(candidate_revision=None),
    lambda generation: generation.update(inventory_count=None),
    lambda generation: generation.pop('native_proof_sha256'),
    lambda generation: generation.update(native_proof_sha256='e' * 64),
])
def test_native_proof_rejects_missing_or_contradictory_nested_generation_receipt(mutation):
    value = native_sample()
    mutation(value['database']['generation'])
    with pytest.raises(ValueError, match='database (nested schema|or owner state)'):
        validate_native_proof(
            value, backend_sha='a' * 64,
            xray_image='sha256:' + 'b' * 64, mihomo_image='sha256:' + 'c' * 64,
        )


def test_native_proof_top_digest_must_bind_to_nested_generation_receipt_digest():
    value = native_sample()
    value['native_proof_sha256'] = 'e' * 64
    with pytest.raises(ValueError, match='database or owner state'):
        validate_native_proof(
            value, backend_sha='a' * 64,
            xray_image='sha256:' + 'b' * 64, mihomo_image='sha256:' + 'c' * 64,
        )


def test_native_proof_image_or_source_substitution_is_rejected():
    value = native_sample()
    value['xray']['runtime_image_digest'] = 'sha256:' + 'e' * 64
    with pytest.raises(ValueError, match='Xray runtime identity'):
        validate_native_proof(
            value, backend_sha='a' * 64,
            xray_image='sha256:' + 'b' * 64, mihomo_image='sha256:' + 'c' * 64,
        )


def test_native_proof_cannot_claim_a_git_sha_for_content_manifest_identity():
    value = native_sample()
    value['source_identity']['commit_sha'] = 'a' * 40
    with pytest.raises(ValueError, match='backend identity'):
        validate_native_proof(
            value, backend_sha='a' * 64,
            xray_image='sha256:' + 'b' * 64, mihomo_image='sha256:' + 'c' * 64,
        )


def test_public_contract_requires_verified_privileged_execution_identity():
    trust = json.loads(CONTRACT.read_text())["privileged_execution_trust"]
    assert trust == {
        "fixed_executable_paths": True,
        "root_owned_regular_files": True,
        "reject_group_or_world_writable": True,
        "trusted_parent_directories": True,
        "bind_verified_file_to_execution": True,
        "os_runtime_closure_separate_gate": True,
        "automatic_host_pin_approval": False,
        "malicious_root_peer_excluded": True,
    }


@pytest.mark.parametrize("invalid_version", [True, 1.0, "1", None])
def test_native_proof_schema_version_is_exact_integer(invalid_version):
    proof = native_sample()
    proof["proof_schema_version"] = invalid_version
    with pytest.raises(ValueError, match="version"):
        validate_native_proof(proof, backend_sha="a" * 64, xray_image="sha256:" + "b" * 64, mihomo_image="sha256:" + "c" * 64)


@pytest.mark.parametrize("mutation", ["extra", "reordered", "duplicate"])
def test_native_proof_v1_checklist_is_exact_and_ordered(mutation):
    proof = native_sample()
    if mutation == "extra":
        proof["checks_executed"].append("unknown_future_check")
    elif mutation == "reordered":
        proof["checks_executed"].reverse()
    else:
        proof["checks_executed"].append(proof["checks_executed"][0])
    with pytest.raises(ValueError, match="checks"):
        validate_native_proof(proof, backend_sha="a" * 64, xray_image="sha256:" + "b" * 64, mihomo_image="sha256:" + "c" * 64)


@pytest.mark.parametrize('size', [0, -1, True, 1.0, '8192', MAX_ARTIFACT_BYTES + 1])
def test_main_evidence_artifact_size_is_bounded_and_strictly_integer(size):
    sha, run, jobs, artifact = valid_main_api_snapshot()
    artifact['size_in_bytes'] = size
    with pytest.raises(ValueError):
        validate_main_ci_provenance(run, jobs, artifact, expected_sha=sha,
                                    expected_attempt=2,
                                    now=datetime(2026, 10, 23, tzinfo=timezone.utc))


def test_main_evidence_artifact_exact_maximum_is_permitted():
    sha, run, jobs, artifact = valid_main_api_snapshot()
    artifact['size_in_bytes'] = MAX_ARTIFACT_BYTES
    validate_main_ci_provenance(run, jobs, artifact, expected_sha=sha,
                                expected_attempt=2,
                                now=datetime(2026, 10, 23, tzinfo=timezone.utc))
