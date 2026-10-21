#!/usr/bin/env python3
"""Immutable cross-runner orchestration; existing aggregate_hosted remains authoritative."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate
import aggregate_hosted as strict

ROLES = ('plan', 'build', 'unit', 'hosted', 'provider', 'packet', 'native')
MAX_FILES = 6000
MAX_BYTES = 256 * 1024 * 1024


def identity(plan):
    run_id, attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not run_id.isdigit() or not attempt.isdigit():
        raise gate.GateError('workflow run/attempt identity is required')
    return {'source_commit': plan['source_commit'], 'plan_digest': plan['plan_digest'],
            'manifest_digest': plan['manifest_digest'], 'manifest_version': plan['manifest_version'],
            'run_id': run_id, 'run_attempt': attempt}


def load_plan(path):
    plan = strict.read_json(Path(path))
    gate.ensure_plan(plan, gate.load_manifest())
    expected = os.environ.get('FWROUTER_CI_PLAN_DIGEST')
    if expected and expected != plan['plan_digest']:
        raise gate.GateError('downloaded plan differs from preflight job output')
    return plan


def requirements(plan, main_smoke=False):
    rows = [r for r in gate.load_manifest()['test_files'] if r['path'] in plan['selected_files']]
    profiles = {r.get('execution_profile') for r in rows}
    return {'plan': True, 'build': True, 'unit': True,
            'hosted': 'hosted-isolated-compose' in profiles or main_smoke,
            'provider': 'hosted-isolated-compose' in profiles,
            'packet': 'hosted-kernel-packet' in profiles,
            'native': any(r.get('execution_profile') == 'qualified-child-process' for r in rows)}


def file_inventory(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise gate.GateError('producer artifact directory is missing or unsafe')
    result = {}; total = 0
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise gate.GateError('symlink in producer artifact')
        if p.is_dir():
            continue
        if not stat.S_ISREG(p.stat().st_mode):
            raise gate.GateError('special file in producer artifact')
        if p.name == 'producer.json' and p.parent == root:
            continue
        relative = p.relative_to(root).as_posix()
        size = p.stat().st_size; total += size
        if len(result) >= MAX_FILES or total > MAX_BYTES:
            raise gate.GateError('producer artifact exceeds file/byte bound')
        result[relative] = {'bytes': size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
    return result


def seal(root, role, plan, outcome):
    if role not in ROLES or outcome not in {'success', 'failure', 'cancelled', 'skipped'}:
        raise gate.GateError('invalid producer role/outcome')
    value = {'schema': 'fwrouter-parallel-producer/v1', 'role': role,
             **identity(plan), 'outcome': outcome, 'files': file_inventory(root)}
    value['producer_digest'] = gate.canonical_digest(value)
    gate.atomic_json(Path(root) / 'producer.json', value)
    return value


def verify_producer(root, role, plan):
    value = strict.read_json(Path(root) / 'producer.json')
    strict.verify_digest(value, 'producer_digest', 'producer envelope')
    if (value.get('schema') != 'fwrouter-parallel-producer/v1'
            or value.get('role') != role or value.get('outcome') != 'success'
            or any(value.get(k) != v for k, v in identity(plan).items())):
        raise gate.GateError(f'{role}: producer identity/status does not match this workflow attempt')
    if value.get('files') != file_inventory(root):
        raise gate.GateError(f'{role}: missing, duplicated, extra or modified producer files')
    for name in value['files']:
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts:
            raise gate.GateError('unsafe artifact relative path')
    return value


def validate_results(results, required):
    problems = []
    if set(results) != set(ROLES):
        problems.append('upstream job result set is missing or unexpected')
    for role, needed in required.items():
        row = results.get(role, {})
        status = row.get('result') if isinstance(row, dict) else None
        if needed and status != 'success':
            problems.append(f'{role}: required upstream result is {status or "missing"}')
        elif not needed and status != 'skipped':
            problems.append(f'{role}: unselected profile must be skipped, got {status or "missing"}')
    return problems


def configure(args):
    plan = load_plan(args.plan); required = requirements(plan, args.main_smoke)
    outputs = {'source_sha': plan['source_commit'], 'plan_digest': plan['plan_digest'],
               **{f'needs_{k}': str(v).lower() for k, v in required.items()},
               'needs_runtime': str(any(required[k] for k in ('hosted', 'provider', 'packet', 'native'))).lower(),
               'needs_recovery': str({'installer/tests/test_safe_recovery.py', 'backend/tests/test_network_contract.py',
                                      'backend/tests/test_dnsmasq.py', 'tests/application_acceptance/test_packet_dataplane.py'}
                                     <= set(plan['selected_files'])).lower(),
               'main_smoke': str(args.main_smoke).lower()}
    with Path(args.github_output).open('a') as stream:
        for k, v in outputs.items():
            stream.write(f'{k}={v}\n')
    seal(Path(args.plan).parent, 'plan', plan, 'success')


def run_command(command, log, *, env=None):
    started = time.monotonic()
    limit = 4 * 1024 * 1024
    total = position = 0
    with Path(log).open('w+b') as stream:
        with subprocess.Popen(['/usr/bin/time', '-v', '-o', str(log) + '.time', *command],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env) as process:
            while block := process.stdout.read(65536):
                offset = 0
                while offset < len(block):
                    count = min(len(block) - offset, limit - position)
                    stream.seek(position)
                    stream.write(block[offset:offset + count])
                    stream.flush()
                    position = (position + count) % limit
                    offset += count
                    total += count
            code = process.wait()
        stream.seek(0)
        raw = stream.read(limit)
        if total >= limit:
            raw = raw[position:] + raw[:position]
        stream.seek(0)
        stream.write(raw)
        stream.truncate()
    print(raw[-65536:].decode(errors='replace'), flush=True)
    return code, round(time.monotonic() - started, 3)


def resource_snapshot(root):
    """Runner capacity and point-in-time usage; process peaks live in *.time."""
    disk = shutil.disk_usage(root)
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}:
            memory[key + '_bytes'] = int(value.split()[0]) * 1024
    return {'cpu_count': os.cpu_count(), 'memory': memory,
            'disk_total_bytes': disk.total, 'disk_used_bytes': disk.used,
            'disk_free_bytes': disk.free}


def run_worker(args):
    plan = load_plan(args.plan); root = Path(args.root); root.mkdir(parents=True, exist_ok=True)
    verify_producer(Path(args.plan).parent, 'plan', plan)
    if args.role != 'unit':
        build = Path(os.environ['RUNNER_TEMP']) / 'build'
        verify_producer(build, 'build', plan)
        import ci_inputs
        ci_inputs.verify(build, Path(args.plan))
    env = dict(os.environ); env['FWROUTER_ACCEPTANCE_PLAN_DIGEST'] = plan['plan_digest']
    gate.atomic_json(root / 'resources-before.json', resource_snapshot(root))
    times = []; status = 0
    if args.role == 'unit':
        commands = [('unit', [sys.executable, 'tests/gates/gate.py', 'run', '--plan', str(args.plan),
                              '--include-native', '--defer-hosted', '--output', str(root / 'fwrouter-affected-unit.json')])]
    else:
        temp = Path(os.environ['RUNNER_TEMP']); env_file = temp / f'fwrouter-{args.role}-runtime.env'
        env_file.touch(mode=0o600, exist_ok=False)
        code, elapsed = run_command([sys.executable, 'tests/acceptance/provision.py', '--runner-temp', str(temp),
                                    '--github-env', str(env_file)], root / 'provision.log', env=env)
        times.append({'stage': 'provision', 'seconds': elapsed, 'exit': code})
        if code:
            gate.atomic_json(root / 'timings.json', times)
            return code
        for line in env_file.read_text().splitlines():
            key, value = line.split('=', 1)
            if not key.startswith('FWROUTER_ACCEPTANCE_'):
                raise gate.GateError('unexpected provision environment output')
            env[key] = value
        shutil.copyfile(temp / 'fwrouter-acceptance-inputs/inputs.json', root / 'runtime-inputs.json')
        if args.role == 'hosted':
            suites = ['functional-ordinary'] if requirements(plan)['provider'] else ['xray-diagnostic']
            commands = [(s, [sys.executable, 'tests/acceptance/launcher.py', '--run', '--suite', s]) for s in suites]
        elif args.role == 'provider':
            commands = [('provider-cohort', [sys.executable, 'tests/acceptance/launcher.py', '--run', '--suite', 'provider-cohort'])]
        elif args.role == 'packet':
            commands = [('packet-diagnostic', [sys.executable, 'tests/acceptance/launcher.py', '--run', '--suite', 'packet-diagnostic'])]
        else:
            rows = [r for r in gate.load_manifest()['test_files'] if r['path'] in plan['selected_files']]
            commands = []
            if any(r.get('execution_profile') == 'qualified-child-process' and r['path'] != 'backend/tests/test_xray_native_readback.py' for r in rows):
                commands.append(('qcp', [sys.executable, 'tests/acceptance/qualified_child.py', '--run']))
            if 'backend/tests/test_xray_native_readback.py' in plan['selected_files']:
                commands.append(('xray', [sys.executable, 'tests/acceptance/qualified_xray_docker.py', '--run']))
    for label, command in commands:
        code, elapsed = run_command(command, root / (label + '.log'), env=env)
        times.append({'stage': label, 'seconds': elapsed, 'exit': code})
        status = status or code
    if args.role != 'unit':
        for pattern in ('fwrouter-acceptance-*-artifacts', 'fwrouter-qcp-*-artifacts', 'fwrouter-xray-native-*-artifacts'):
            for source in Path(os.environ['RUNNER_TEMP']).glob(pattern):
                if source.is_dir():
                    shutil.copytree(source, root / source.name)
    gate.atomic_json(root / 'timings.json', times)
    gate.atomic_json(root / 'resources-after.json', resource_snapshot(root))
    return status


def one(root, pattern):
    paths = list(Path(root).rglob(pattern))
    if len(paths) != 1:
        raise gate.GateError(f'exactly one {pattern} required, observed {len(paths)}')
    return paths[0]


def aggregate(args):
    output = Path(args.root); output.mkdir(parents=True, exist_ok=True)
    results = {}
    try:
        results = json.loads(os.environ.get('FWROUTER_CI_NEEDS', '{}'))
        if not isinstance(results, dict):
            raise gate.GateError('upstream job results must be an object')
        plan_row = results.get('plan', {})
        plan_status = plan_row.get('result') if isinstance(plan_row, dict) else None
        if plan_status != 'success':
            raise gate.GateError(f'plan preflight result is {plan_status or "missing"}; immutable plan unavailable')
        plan = load_plan(args.plan)
        required = requirements(plan, args.main_smoke)
        problems = validate_results(results, required)
        if problems:
            raise gate.GateError('; '.join(problems))
        artifact_root = Path(args.artifacts)
        prefix = f"fwrouter-v2-{{role}}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
        expected_dirs = {prefix.format(role=k) for k, needed in required.items() if needed}
        if {p.name for p in artifact_root.iterdir()} != expected_dirs:
            raise gate.GateError('missing or unexpected producer artifact directories')
        for role in ROLES:
            source = artifact_root / prefix.format(role=role)
            if source.exists():
                source.rename(artifact_root / role)
        args.plan = artifact_root / 'plan' / Path(args.plan).name
        for role, needed in required.items():
            if needed:
                directory = Path(args.plan).parent if role == 'plan' else artifact_root / role
                verify_producer(directory, role, plan)
            elif (artifact_root / role).exists():
                raise gate.GateError(f'unexpected artifact for unselected profile {role}')
        import ci_inputs
        ci_inputs.verify(artifact_root / 'build', Path(args.plan))
        manifests = [strict.read_json(one(artifact_root / k, 'runtime-inputs.json'))
                     for k in ('hosted', 'provider', 'packet', 'native') if required[k]]
        for manifest in manifests:
            strict.validate_runtime_inputs(manifest)
        runtime = None
        if manifests:
            runtime = output / 'runtime-inputs.json'; gate.atomic_json(runtime, manifests[0])
        hosted = []
        for kind in ('hosted', 'provider', 'packet'):
            if required[kind]:
                hosted.extend((artifact_root / kind).rglob('hosted-acceptance-report.json'))
        qcp = list((artifact_root / 'native').rglob('qualified-child-receipt.json')) if required['native'] else []
        xray = list((artifact_root / 'native').rglob('qualified-xray-docker-receipt.json')) if required['native'] else []
        smoke = None
        if args.main_smoke:
            suite = 'functional-ordinary' if requirements(plan)['provider'] else 'xray-diagnostic'
            candidates = [p for p in (artifact_root / 'hosted').rglob('hosted-acceptance-report.json')
                          if strict.read_json(p).get('suite') == suite]
            if len(candidates) != 1:
                raise gate.GateError('required main smoke receipt missing or duplicated')
            smoke = candidates[0]
            if suite == 'xray-diagnostic':
                hosted = [p for p in hosted if p != smoke]
        report = strict.aggregate(Path(args.plan), one(artifact_root / 'unit', 'fwrouter-affected-unit.json'),
                                  hosted, qcp, xray, minimum_repeats=1, runtime_inputs_path=runtime,
                                  require_main_smoke=args.main_smoke, main_smoke_path=smoke)
        gate.atomic_json(output / 'fwrouter-aggregate.json', report)
        if report.get('eligible') is not True:
            return 1
        recovery = {'installer/tests/test_safe_recovery.py', 'backend/tests/test_network_contract.py',
                    'backend/tests/test_dnsmasq.py', 'tests/application_acceptance/test_packet_dataplane.py'} <= set(plan['selected_files'])
        if recovery and required['packet']:
            subprocess.run([sys.executable, 'installer/qualify_recovery.py', '--aggregate',
                            str(output / 'fwrouter-aggregate.json'), '--output', str(output / 'safe-recovery')], check=True)
        gate.atomic_json(output / 'required-result.json', {'schema': 'fwrouter-parallel-required/v1',
                         **identity(plan), 'eligible': True, 'upstream': results, 'safe_recovery_required': recovery})
        return 0
    except (OSError, ValueError, gate.GateError, subprocess.CalledProcessError) as exc:
        gate.atomic_json(output / 'required-result.json', {'schema': 'fwrouter-parallel-required/v1',
                         'eligible': False, 'upstream': results, 'error': str(exc)[:3000]})
        if not (output / 'fwrouter-aggregate.json').exists():
            gate.atomic_json(output / 'fwrouter-aggregate.json', strict.aggregate_error_report(exc))
        print(str(exc), file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('configure', 'verify-plan', 'seal', 'worker', 'aggregate'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--role', choices=ROLES)
    parser.add_argument('--outcome', default='failure')
    parser.add_argument('--github-output', type=Path)
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--main-smoke', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            configure(args)
        elif args.command == 'verify-plan':
            verify_producer(args.plan.parent, 'plan', load_plan(args.plan))
        elif args.command == 'seal':
            seal(args.root, args.role, load_plan(args.plan), args.outcome)
        elif args.command == 'worker':
            return run_worker(args)
        else:
            return aggregate(args)
        return 0
    except (OSError, ValueError, gate.GateError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
