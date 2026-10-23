#!/usr/bin/env python3
"""Parallel full regression orchestration and independently replayable L6 evidence."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate
import parallel_ci as parallel
import aggregate_hosted as strict
import evidence_bundle as bundle
import routine_shards
import visibility_report
import ci_inputs

ROLES = parallel.ROLES
HOSTED = ('hosted', 'provider', 'packet')
REPEATS = ('1', '2')
WORKFLOW = 'FWRouter L6 full regression'
SOURCES = (*bundle.SOURCE_FILES, 'tests/gates/l6_ci.py', '.github/workflows/test-full-suite.yml')
SCHEMA = 'fwrouter-l6-evidence/v1'


def load(path):
    return parallel.load_plan(path, l6=True)


def trigger(event, ref, acknowledge, mode, expected_sha, actual_sha):
    if event == 'schedule':
        if ref != 'refs/heads/main':
            raise gate.GateError('nightly L6 requires main')
        return 'main-regression'
    if (event != 'workflow_dispatch' or acknowledge != 'RUN L6'
            or expected_sha != actual_sha or len(actual_sha) != 40):
        raise gate.GateError('L6 dispatch acknowledgement or exact SHA is invalid')
    if mode == 'main' and ref == 'refs/heads/main':
        return 'main-regression'
    if mode == 'branch' and ref.startswith('refs/heads/') and ref != 'refs/heads/main':
        return 'branch-regression-non-promotional'
    raise gate.GateError('L6 mode/ref is not authorized by the trigger contract')


def configure(args):
    scope = trigger(os.environ['GITHUB_EVENT_NAME'], os.environ['GITHUB_REF'],
                    args.acknowledge, args.mode, args.expected_sha, gate.git('rev-parse', 'HEAD'))
    plan = load(args.plan)
    parallel.seal(args.plan.parent, 'plan', plan, 'success')
    with Path(args.github_output).open('a') as stream:
        for key, value in {'source_sha': plan['source_commit'], 'plan_digest': plan['plan_digest'],
                           'unit_shards_json': json.dumps(gate.ROUTINE_SHARD_GROUPS),
                           'native_profiles_json': json.dumps(parallel.native_profiles(plan)),
                           'qualification_scope': scope}.items():
            stream.write(f'{key}={value}\n')


def expected_children():
    return {'plan': ('plan',), 'build': ('build',),
            'unit': tuple('unit-' + group for group in gate.ROUTINE_SHARD_GROUPS),
            **{role: tuple(role + '-r' + repeat for repeat in REPEATS) for role in HOSTED},
            'native': ('native-qcp', 'native-xray')}


def check_jobs(jobs):
    if not isinstance(jobs, dict) or set(jobs) != set(ROLES):
        raise gate.GateError('L6 required job result set is incomplete or unexpected')
    failed = [role for role in ROLES if not isinstance(jobs[role], dict)
              or jobs[role].get('result') != 'success']
    if failed:
        raise gate.GateError('L6 required jobs failed/skipped/cancelled: ' + ', '.join(failed))


def check_native_files(root, profile, plan):
    parallel.verify_producer(root, 'native', plan)
    value = strict.read_json(root / 'producer.json')
    files = value['files']
    name = 'qualified-child-receipt.json' if profile == 'qcp' else 'qualified-xray-docker-receipt.json'
    receipts = [path for path in files if Path(path).name == name]
    if len(receipts) != 1 or set(files) != {'runtime-inputs.json', receipts[0], str(Path(receipts[0]).with_name('profile.json'))}:
        raise gate.GateError('L6 native child has missing or unapproved evidence')


def replay(evidence, plan, jobs):
    check_jobs(jobs)
    children = expected_children()
    manifests = []
    for role, names in children.items():
        for child in names:
            root = evidence / child
            parallel.verify_producer(root, role, plan)
            files = strict.read_json(root / 'producer.json')['files']
            if role == 'plan' and set(files) != {'fwrouter-affected-plan.json'}:
                raise gate.GateError('unexpected L6 planner evidence')
            if role == 'unit' and set(files) != {'shard.json'}:
                raise gate.GateError('unexpected L6 routine evidence')
            if role in HOSTED:
                receipts = [name for name in files if Path(name).name == 'hosted-acceptance-report.json']
                if len(receipts) != 1 or set(files) != {'runtime-inputs.json', receipts[0]}:
                    raise gate.GateError('unexpected L6 hosted evidence')
            if role == 'native':
                check_native_files(root, child.removeprefix('native-'), plan)
            if role in (*HOSTED, 'native'):
                runtime = strict.read_json(root / 'runtime-inputs.json')
                strict.validate_runtime_inputs(runtime)
                manifests.append(runtime)
    if any(parallel.runtime_identity(value) != parallel.runtime_identity(manifests[0]) for value in manifests[1:]):
        raise gate.GateError('L6 runners disagree on immutable runtime inputs')
    ci_inputs.verify(evidence / 'build', evidence / 'plan/fwrouter-affected-plan.json')
    shards = {group: strict.read_json(evidence / ('unit-' + group) / 'shard.json')
              for group in gate.ROUTINE_SHARD_GROUPS}
    merged = routine_shards.merge_reports(plan, gate.load_manifest(), shards,
                                         run_id=os.environ['GITHUB_RUN_ID'], attempt=os.environ['GITHUB_RUN_ATTEMPT'])
    with tempfile.TemporaryDirectory(prefix='fwrouter-l6-replay-') as tmp:
        unit_path = Path(tmp) / 'fwrouter-affected-unit.json'
        runtime_path = Path(tmp) / 'runtime-inputs.json'
        gate.atomic_json(unit_path, merged)
        gate.atomic_json(runtime_path, manifests[0])
        hosted = [parallel.one(evidence / name, 'hosted-acceptance-report.json')
                  for role in HOSTED for name in children[role]]
        qcp = [parallel.one(evidence / 'native-qcp', 'qualified-child-receipt.json')]
        xray = [parallel.one(evidence / 'native-xray', 'qualified-xray-docker-receipt.json')]
        result = strict.aggregate(evidence / 'plan/fwrouter-affected-plan.json', unit_path,
                                  hosted, qcp, xray, minimum_repeats=2, runtime_inputs_path=runtime_path)
    if result.get('eligible') is not True or result.get('hosted_repeats_required') != 2:
        raise gate.GateError('L6 strict regression aggregate is not eligible: ' + str(result.get('blocked_reasons', []))[:1500])
    return merged, result


def prepare_visibility(evidence, required, plan, merged):
    # Existing visibility reader uses one canonical unit path. The other
    # original handoffs remain unchanged and are independently sealed.
    unit = evidence / 'unit'
    unit.mkdir(exist_ok=True)
    gate.atomic_json(unit / 'fwrouter-affected-unit.json', merged)
    report = visibility_report.collect(evidence, required, plan)
    if report['qualification_status'] != 'passed' or report['not_run_selected_files'] or report['diagnostic_limitations']:
        raise gate.GateError('L6 observed execution coverage is incomplete')
    gate.atomic_json(required / 'visibility.json', report)
    return report


def aggregate(args):
    required = args.root
    required.mkdir(mode=0o700, parents=True, exist_ok=True)
    jobs = json.loads(os.environ.get('FWROUTER_CI_NEEDS', '{}'))
    try:
        check_jobs(jobs)
        suffix = f"-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
        expected = {'fwrouter-l6-' + child + suffix for names in expected_children().values() for child in names}
        if {path.name for path in args.artifacts.iterdir()} != expected:
            raise gate.GateError('L6 producer artifacts missing, duplicated or from another attempt')
        for names in expected_children().values():
            for child in names:
                (args.artifacts / ('fwrouter-l6-' + child + suffix)).rename(args.artifacts / child)
        plan = load(args.artifacts / 'plan/fwrouter-affected-plan.json')
        merged, result = replay(args.artifacts, plan, jobs)
        gate.atomic_json(required / 'fwrouter-aggregate.json', result)
        subprocess.run([sys.executable, 'installer/qualify_recovery.py', '--aggregate',
                        str(required / 'fwrouter-aggregate.json'), '--output', str(required / 'safe-recovery')], check=True)
        gate.atomic_json(required / 'required-result.json', {'schema': 'fwrouter-l6-required/v1',
                         **parallel.identity(plan), 'eligible': True, 'upstream': jobs,
                         'qualification_scope': args.scope, 'minimum_repeats': 2,
                         'safe_recovery_required': True})
        prepare_visibility(args.artifacts, required, plan, merged)
        return 0
    except (OSError, ValueError, KeyError, gate.GateError, subprocess.CalledProcessError) as exc:
        gate.atomic_json(required / 'required-result.json', {'schema': 'fwrouter-l6-required/v1',
                         'eligible': False, 'upstream': jobs, 'error': str(exc)[:3000]})
        print(str(exc), file=sys.stderr)
        return 1


def copy_bundle(args):
    plan = load(args.artifacts / 'plan/fwrouter-affected-plan.json')
    saved = strict.read_json(args.root / 'required-result.json')
    if saved.get('eligible') is not True or saved.get('minimum_repeats') != 2:
        raise gate.GateError('ineligible L6 cannot publish successful evidence')
    merged, result = replay(args.artifacts, plan, saved['upstream'])
    if gate.canonical_digest(result) != gate.canonical_digest(strict.read_json(args.root / 'fwrouter-aggregate.json')):
        raise gate.GateError('L6 replay differs from retained aggregate')
    if args.bundle.exists():
        raise gate.GateError('L6 bundle destination must be fresh')
    args.bundle.mkdir(mode=0o700, parents=True)
    for names in expected_children().values():
        for child in names:
            source = args.artifacts / child
            for path in source.rglob('*'):
                if path.is_file():
                    bundle.copy_evidence_file(path, args.bundle / 'evidence' / child / path.relative_to(source))
    for name in ('required-result.json', 'fwrouter-aggregate.json', 'visibility.json'):
        bundle.copy_evidence_file(args.root / name, args.bundle / 'reports' / name)
    for name in ('qualification.json', 'stage.json', 'artifact/manifest.json', 'artifact/source.tar'):
        bundle.copy_evidence_file(args.root / 'safe-recovery' / name, args.bundle / 'reports/safe-recovery' / name)
    sources = []
    for name in SOURCES:
        source = gate.ROOT / name
        target = args.bundle / 'verifier-source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        sources.append({'path': name, 'sha256': bundle.sha256(source)})
    metadata = {'schema': SCHEMA, 'repository': 'Coxf0rd/fwrouter', 'workflow': WORKFLOW,
                'workflow_path': '.github/workflows/test-full-suite.yml', **parallel.identity(plan),
                'event': os.environ['GITHUB_EVENT_NAME'], 'ref': os.environ['GITHUB_REF'],
                'qualification_scope': saved['qualification_scope'], 'minimum_repeats': 2,
                'deploy_eligibility': False, 'sources': sources}
    gate.atomic_json(args.bundle / 'l6-metadata.json', metadata)
    inventory = bundle.inventory(args.bundle)
    gate.atomic_json(args.bundle / 'content-manifest.json', {'schema': SCHEMA, 'files': inventory})
    return metadata


def verify(args):
    if gate.git('rev-parse', 'HEAD') != args.expected_sha or gate.git('status', '--porcelain', '--untracked-files=all'):
        raise gate.GateError('L6 verification requires a trusted clean exact-source checkout')
    metadata = strict.read_json(args.bundle / 'l6-metadata.json')
    expected = {'schema': SCHEMA, 'repository': 'Coxf0rd/fwrouter', 'workflow': WORKFLOW,
                'workflow_path': '.github/workflows/test-full-suite.yml', 'source_commit': args.expected_sha,
                'run_id': args.run_id, 'run_attempt': args.attempt, 'event': args.event, 'ref': args.ref,
                'minimum_repeats': 2, 'deploy_eligibility': False}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise gate.GateError('L6 bundle identity differs from independent expectations')
    if args.event not in {'schedule', 'workflow_dispatch'} or not args.ref.startswith('refs/heads/'):
        raise gate.GateError('invalid L6 event/ref')
    scope = 'main-regression' if args.ref == 'refs/heads/main' else 'branch-regression-non-promotional'
    if metadata.get('qualification_scope') != scope or args.event == 'schedule' and scope != 'main-regression':
        raise gate.GateError('L6 promotion scope differs from event/ref')
    inventory = strict.read_json(args.bundle / 'content-manifest.json')
    if inventory.get('schema') != SCHEMA or inventory.get('files') != bundle.inventory(args.bundle):
        raise gate.GateError('L6 evidence file inventory is missing, modified or unexpected')
    expected_paths = {'l6-metadata.json', 'content-manifest.json',
                      'reports/required-result.json', 'reports/fwrouter-aggregate.json', 'reports/visibility.json',
                      'reports/safe-recovery/qualification.json', 'reports/safe-recovery/stage.json',
                      'reports/safe-recovery/artifact/manifest.json', 'reports/safe-recovery/artifact/source.tar'}
    expected_paths.update('verifier-source/' + name for name in SOURCES)
    for names in expected_children().values():
        for child in names:
            producer = strict.read_json(args.bundle / 'evidence' / child / 'producer.json')
            expected_paths.add('evidence/' + child + '/producer.json')
            for name in producer.get('files', {}):
                bundle.safe_path(name)
                expected_paths.add('evidence/' + child + '/' + name)
    if {row['path'] for row in inventory['files']} | {'content-manifest.json'} != expected_paths:
        raise gate.GateError('L6 bundle has missing or unexpected file paths')
    sources = [{'path': name, 'sha256': bundle.sha256(gate.ROOT / name)} for name in SOURCES]
    if metadata.get('sources') != sources:
        raise gate.GateError('L6 trusted source inventory mismatch')
    for row in sources:
        if bundle.sha256(args.bundle / 'verifier-source' / row['path']) != row['sha256']:
            raise gate.GateError('L6 bundled verifier source mismatch')
    os.environ['GITHUB_RUN_ID'] = args.run_id
    os.environ['GITHUB_RUN_ATTEMPT'] = args.attempt
    evidence = args.bundle / 'evidence'
    plan = load(evidence / 'plan/fwrouter-affected-plan.json')
    result = strict.read_json(args.bundle / 'reports/required-result.json')
    if (result.get('schema') != 'fwrouter-l6-required/v1' or result.get('eligible') is not True
            or result.get('minimum_repeats') != 2 or result.get('qualification_scope') != scope
            or result.get('safe_recovery_required') is not True
            or any(result.get(key) != value for key, value in parallel.identity(plan).items())):
        raise gate.GateError('L6 required result is incomplete or stale')
    merged, replayed = replay(evidence, plan, result['upstream'])
    if gate.canonical_digest(replayed) != gate.canonical_digest(strict.read_json(args.bundle / 'reports/fwrouter-aggregate.json')):
        raise gate.GateError('L6 strict offline replay mismatch')
    import importlib.util
    spec = importlib.util.spec_from_file_location('l6_safe_recovery', gate.ROOT / 'installer/safe_recovery.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    recovery = args.bundle / 'reports/safe-recovery'
    module.verify(recovery / 'artifact', recovery / 'qualification.json')
    stage = strict.read_json(recovery / 'stage.json')
    if stage.get('status') != 'isolated_stage_complete' or stage.get('source_sha') != args.expected_sha:
        raise gate.GateError('L6 safe recovery staging is incomplete')
    with tempfile.TemporaryDirectory(prefix='fwrouter-l6-visibility-') as tmp:
        scratch = Path(tmp)
        # Replay visibility without modifying the input bundle.
        view = scratch / 'evidence'; view.mkdir()
        for names in expected_children().values():
            for child in names:
                (view / child).symlink_to(evidence / child, target_is_directory=True)
        unit = view / 'unit'; unit.mkdir(); gate.atomic_json(unit / 'fwrouter-affected-unit.json', merged)
        # Path.rglob does not recurse symlink directories; copy only sealed small
        # receipt trees for this diagnostic recomputation.
        for role in (*HOSTED, 'native'):
            for child in expected_children()[role]:
                (view / child).unlink(); shutil.copytree(evidence / child, view / child)
        observed = visibility_report.collect(view, args.bundle / 'reports', plan)
    if gate.canonical_digest(observed) != gate.canonical_digest(strict.read_json(args.bundle / 'reports/visibility.json')):
        raise gate.GateError('L6 observed coverage replay mismatch')
    return {'status': 'l6_evidence_verified_not_deploy_authorized', 'source_commit': args.expected_sha,
            'run_id': args.run_id, 'run_attempt': args.attempt, 'scope': scope}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('configure', 'aggregate', 'bundle', 'verify'))
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--github-output', type=Path)
    parser.add_argument('--acknowledge', default='')
    parser.add_argument('--mode', default='main')
    parser.add_argument('--expected-sha', default='')
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--scope', default='branch-regression-non-promotional')
    parser.add_argument('--run-id', default='')
    parser.add_argument('--attempt', default='')
    parser.add_argument('--event', default='')
    parser.add_argument('--ref', default='')
    args = parser.parse_args()
    try:
        if args.command == 'configure':configure(args)
        elif args.command == 'aggregate':return aggregate(args)
        elif args.command == 'bundle':print(json.dumps(copy_bundle(args)))
        else:print(json.dumps(verify(args)))
        return 0
    except (OSError, ValueError, KeyError, gate.GateError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
