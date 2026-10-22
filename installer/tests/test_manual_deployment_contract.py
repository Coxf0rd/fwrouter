"""Public data-contract tests; never import or execute the private root wrapper."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

CONTRACT = Path(__file__).parent / 'fixtures/manual-deployment-contract.json'


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
        'local_source', 'remote_main', 'ci_v2', 'preflight', 'native_proof',
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
                'local_source', 'remote_main', 'ci_v2', 'preflight', 'native_proof',
                'ui_integrity', 'installer', 'postflight',
            ], 'PASS'),
            'private_qualification': 'separate-private-hash-bound-evidence'}


REQUIRED_NATIVE_CHECKS = {
    'backend_manifest_sha256', 'sqlite_schema_integrity_foreign_keys',
    'generation_owner_state', 'checkpoint_state',
    'xray_identity_bindings_config_runtime', 'mihomo_config_rules_selectors_runtime',
    'core_manifest_nft_ip_routing_runtime', 'final_native_runtime_fence_recheck',
    'generation_native_receipt_exact_match',
}


def validate_native_proof(value: dict, *, backend_sha: str, xray_image: str, mihomo_image: str) -> None:
    """Check the public CLI contract; this synthetic validator grants no deployment authority."""
    required = {
        'proof_schema_version', 'checks_executed', 'scope', 'source_identity', 'database',
        'xray', 'mihomo', 'core', 'status', 'reason', 'native_proof_sha256',
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('native proof shape')
    if value['proof_schema_version'] != 1 or value['scope'] != 'ui':
        raise ValueError('native proof version or scope')
    if value['status'] != 'PASS' or value['reason'] is not None:
        raise ValueError('native proof is not PASS')
    source = value['source_identity']
    if source != {'kind': 'backend_manifest_sha256', 'sha256': backend_sha, 'commit_sha': None}:
        raise ValueError('backend identity')
    checks = value['checks_executed']
    if (not isinstance(checks, list) or not all(isinstance(item, str) for item in checks)
            or len(checks) != len(set(checks)) or not REQUIRED_NATIVE_CHECKS <= set(checks)):
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
    assert contract['schema'] == 'fwrouter-manual-deployment-contract/v2'
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


def test_all_40_safety_scenarios_are_declared_without_production_execution():
    scenarios = json.loads(CONTRACT.read_text())['required_scenarios']
    assert [x['id'] for x in scenarios] == list(range(1, 41))
    assert len({x['name'] for x in scenarios}) == 40
    assert all(x['production_allowed'] is False for x in scenarios)


def test_log_retention_cannot_cover_permanent_receipts():
    contract = json.loads(CONTRACT.read_text())
    assert contract['temporary_log_days'] == 7
    assert contract['permanent_receipts_in_tmpfiles_scope'] is False


def test_complete_redacted_summary_is_valid_data_not_authority():
    validate_public_receipt(sample())


@pytest.mark.parametrize('gate', [
    'local_source', 'remote_main', 'ci_v2', 'preflight', 'native_proof',
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
        'checks_executed': sorted(REQUIRED_NATIVE_CHECKS),
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
