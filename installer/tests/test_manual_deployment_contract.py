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
    if not isinstance(gates, dict) or set(gates) != {'local_source', 'remote_main', 'ci_v2', 'preflight', 'installer', 'postflight'}:
        raise ValueError('gate completeness')
    if any(status not in {'PASS', 'FAIL', 'NOT RUN'} for status in gates.values()):
        raise ValueError('gate status')
    if value['status'] not in {'PASS', 'FAIL', 'CHECK ONLY'}:
        raise ValueError('overall status')
    if value['status'] == 'PASS' and any(status != 'PASS' for status in gates.values()):
        raise ValueError('partial success is not deployment success')
    if value['private_qualification'] != 'separate-private-hash-bound-evidence':
        raise ValueError('public CI must not claim private wrapper qualification')


def sample() -> dict:
    return {'schema': 'fwrouter-manual-deployment-public-summary/v1', 'source_sha': 'a' * 40,
            'scope': 'ui', 'status': 'PASS',
            'gates': dict.fromkeys(['local_source', 'remote_main', 'ci_v2', 'preflight', 'installer', 'postflight'], 'PASS'),
            'private_qualification': 'separate-private-hash-bound-evidence'}


def test_public_contract_preserves_owner_control_and_local_source():
    contract = json.loads(CONTRACT.read_text())
    assert contract['execution'] == 'owner-invoked-local-only'
    assert contract['wrapper_distribution'] == 'outside-repository-owner-controlled'
    assert contract['source'] == 'verified-existing-local-git-objects'
    assert contract['remote_purpose'] == 'metadata-and-ci-evidence-only'
    assert contract['automatic_deployment'] is False
    assert contract['initial_scopes'] == ['ui']
    assert {'backend', 'schema', 'recovery', 'host'} <= set(contract['unsupported_scopes'])


def test_all_34_safety_scenarios_are_declared_without_production_execution():
    scenarios = json.loads(CONTRACT.read_text())['required_scenarios']
    assert [x['id'] for x in scenarios] == list(range(1, 35))
    assert len({x['name'] for x in scenarios}) == 34
    assert all(x['production_allowed'] is False for x in scenarios)


def test_log_retention_cannot_cover_permanent_receipts():
    contract = json.loads(CONTRACT.read_text())
    assert contract['temporary_log_days'] == 7
    assert contract['permanent_receipts_in_tmpfiles_scope'] is False


def test_complete_redacted_summary_is_valid_data_not_authority():
    validate_public_receipt(sample())


@pytest.mark.parametrize('gate', ['local_source', 'remote_main', 'ci_v2', 'preflight', 'installer', 'postflight'])
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
