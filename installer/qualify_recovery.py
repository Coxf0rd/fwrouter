#!/usr/bin/env python3
"""Bind admitted hosted evidence to an actual source artifact and isolated stage.

Run only after the strict hosted aggregate. This is not live rollback proof.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

import safe_recovery as recovery


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aggregate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    aggregate = recovery._read_json_file(args.aggregate, 'strict hosted aggregate')
    required_files = {'installer/tests/test_safe_recovery.py',
                      'backend/tests/test_network_contract.py', 'backend/tests/test_dnsmasq.py',
                      'tests/application_acceptance/test_packet_dataplane.py'}
    sha = recovery._git_source_identity(root)
    if (aggregate.get('eligible') is not True or aggregate.get('blocked_reasons')
            or aggregate.get('source_commit') != sha
            or not required_files <= set(aggregate.get('selected_files', []))
            or not {'functional-ordinary', 'provider-cohort', 'packet-diagnostic'} <= set(aggregate.get('hosted_required_partitions', []))):
        raise recovery.RecoveryError('complete recovery qualification gates were not executed')
    digest = recovery._entries_digest(recovery._entries(recovery._tracked_source_files(root)))
    run_id = os.environ['GITHUB_RUN_ID']
    args.output.mkdir(mode=0o700)
    receipt_path = args.output / 'qualification.json'
    receipt = {'schema': recovery.RECEIPT_SCHEMA, 'status': 'passed', 'source_sha': sha,
               'source_manifest_sha256': digest, 'receipt_id': 'strict-aggregate-' + run_id,
               'hosted_run_id': run_id,
               'hosted_run_url': 'https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + run_id,
               'gates': {name: 'passed' for name in recovery.REQUIRED_GATES},
               'scope': 'hosted_source_and_isolated_backend_stage', 'live_activation': 'NOT RUN'}
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    artifact = args.output / 'artifact'
    recovery.prepare(root, artifact, receipt_path)
    with tempfile.TemporaryDirectory(prefix='fwrouter-qualified-recovery-') as private:
        target = Path(private) / 'target'
        target.mkdir(mode=0o700)
        staged = recovery.stage(artifact, receipt_path, target)
    (args.output / 'stage.json').write_text(json.dumps(staged, indent=2) + '\n')
    print(json.dumps({'artifact_source': sha, 'scope': receipt['scope'], 'live_activation': 'NOT RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
