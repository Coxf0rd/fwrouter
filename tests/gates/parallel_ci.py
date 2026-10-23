#!/usr/bin/env python3
"""Immutable cross-runner orchestration; existing aggregate_hosted remains authoritative."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate
import aggregate_hosted as strict
import routine_shards

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


def native_profiles(plan):
    rows = [r for r in gate.load_manifest()['test_files'] if r['path'] in plan['selected_files']]
    profiles = []
    if any(r.get('execution_profile') == 'qualified-child-process'
           and r['path'] != 'backend/tests/test_xray_native_readback.py' for r in rows):
        profiles.append('qcp')
    if any(r['path'] == 'backend/tests/test_xray_native_readback.py' for r in rows):
        profiles.append('xray')
    return profiles


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
               'main_smoke': str(args.main_smoke).lower(),
               'unit_shards_json': json.dumps(list(gate.ROUTINE_SHARD_GROUPS), separators=(',', ':')),
               'native_profiles_json': json.dumps(native_profiles(plan), separators=(',', ':'))}
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


def process_peak(log_path: Path) -> dict[str, int]:
    """Parse only bounded numeric resource fields from GNU time -v output."""
    path = Path(str(log_path) + '.time')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        return {}
    try:
        content = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    result = {}
    for label, key in (('Maximum resident set size (kbytes):', 'max_rss_kib'),
                       ('Percent of CPU this job got:', 'cpu_percent')):
        match = re.search(rf'^{re.escape(label)}\s*(\d+)\s*$', content, re.MULTILINE)
        if match:
            result[key] = int(match.group(1))
    return result


def worker_summary(root: Path, plan_path: Path, role: str, outcome: str,
                   native_profile: str | None = None) -> str:
    """Render bounded, non-payload job summary from local runner receipts."""
    plan = strict.read_json(plan_path)
    lines = [f"## FWRouter CI {role}" + (f" ({native_profile})" if native_profile else ""), "",
             f"- Status: **{outcome.upper()}**", f"- Source SHA: `{plan.get('source_commit', 'unknown')}`",
             f"- Plan digest: `{plan.get('plan_digest', 'unknown')}`",
             f"- Run/attempt: `{os.environ.get('GITHUB_RUN_ID', 'unavailable')}` / "
             f"`{os.environ.get('GITHUB_RUN_ATTEMPT', 'unavailable')}`"]
    selected = set(plan.get('selected_files', []))
    rows = [row for row in gate.load_manifest()['test_files'] if row['path'] in selected]
    if role == 'plan':
        by_owner: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_owner.setdefault(row['execution_group'], []).append(row)
        lines += ["", "| Execution owner | Files | Levels | Domains | Profile files |",
                  "|---|---:|---|---|---:|"]
        for owner in gate.ROUTINE_SHARD_GROUPS:
            owned = by_owner.get(owner, [])
            lines.append(f"| `{owner}` | {len(owned)} | "
                         f"{', '.join(sorted({row['primary_level'] for row in owned})) or 'none'} | "
                         f"{', '.join(sorted({row['domain'] for row in owned})) or 'none'} | "
                         f"{sum(bool(row.get('execution_profile')) for row in owned)} |")
    root = Path(root)
    timings_path = root / 'timings.json'
    if timings_path.is_file() and not timings_path.is_symlink():
        try:
            timings = strict.read_json(timings_path) if timings_path.read_text().lstrip().startswith('{') else json.loads(timings_path.read_text())
            if isinstance(timings, list):
                lines += ["", "| Stage | Result | Seconds | Peak RSS KiB |", "|---|---:|---:|---:|"]
                lines += [f"| {item.get('stage', 'unknown')} | {item.get('exit', 'unknown')} | "
                          f"{float(item.get('seconds', 0)):.3f} | "
                          f"{process_peak(root / (str(item.get('stage', 'unknown')) + '.log')).get('max_rss_kib', 'n/a')} |"
                          for item in timings if isinstance(item, dict)]
        except (OSError, ValueError, TypeError):
            lines += ["", "Stage timings: unavailable or malformed."]
    receipt_paths = []
    for name in ('hosted-acceptance-report.json', 'qualified-child-receipt.json',
                 'qualified-xray-docker-receipt.json', 'build-summary.json', 'wheel-metadata.json'):
        receipt_paths.extend(sorted(root.rglob(name)))
    id_statuses: list[tuple[str, str]] = []
    cleanup_values: list[str] = []
    receipt_rows = []
    for path in receipt_paths:
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
            continue
        try:
            value = strict.read_json(path)
        except (OSError, ValueError, gate.GateError):
            continue
        tests = value.get('tests') if isinstance(value.get('tests'), dict) else {}
        statuses = tests.get('node_status') if isinstance(tests.get('node_status'), dict) else {}
        for node, status in statuses.items():
            if isinstance(node, str) and isinstance(status, str):
                id_statuses.append((node, status))
        cleanup = value.get('cleanup')
        if isinstance(cleanup, str):
            cleanup_values.append(cleanup)
        receipt_rows.append((path.name, str(value.get('suite', value.get('scope', value.get('package', 'unknown')))),
                             str(value.get('status', 'unknown'))))
    build_summary = root / 'build' / 'build-summary.json'
    if role == 'build' and build_summary.is_file() and not build_summary.is_symlink():
        try:
            data = strict.read_json(build_summary)
            build_timings = data.get('timings_seconds', {})
            lines += ["", "| Build check | Seconds |", "|---|---:|"]
            lines += [f"| {key} | {float(value):.3f} |" for key, value in sorted(build_timings.items())
                      if isinstance(value, (int, float)) and not isinstance(value, bool)]
            wheel = data.get('wheel', {})
            lines.append(f"- Wheel: `{wheel.get('bytes', 'unknown')}` bytes; SHA256 `{wheel.get('sha256', 'unavailable')}`.")
        except (OSError, ValueError, TypeError, gate.GateError):
            lines.append("- Build summary unavailable or malformed.")
    if receipt_rows:
        lines += ["", "| Receipt | Suite/profile | Status |", "|---|---|---|"]
        lines += [f"| `{name}` | `{suite}` | `{status}` |" for name, suite, status in receipt_rows]
    lines += ["", f"Observed test IDs: {len(id_statuses)}; unique IDs: {len({node for node, _ in id_statuses})}."]
    if id_statuses:
        lines += ["", "| Test ID | Result |", "|---|---|"]
        lines += [f"| `{node.replace('|', '&#124;')}` | `{status}` |" for node, status in sorted(id_statuses)]
    before = root / 'resources-before.json'
    after = root / 'resources-after.json'
    for label, path in (("before", before), ("after", after)):
        if path.is_file() and not path.is_symlink():
            try:
                data = strict.read_json(path)
                memory = data.get('memory', {})
                lines.append(f"- Runner resources {label}: {data.get('cpu_count', 'unknown')} CPUs; "
                             f"RAM available {memory.get('MemAvailable_bytes', 'unknown')} bytes; "
                             f"disk free {data.get('disk_free_bytes', 'unknown')} bytes.")
            except (OSError, ValueError, gate.GateError):
                lines.append(f"- Runner resources {label}: unavailable.")
    lines.append(f"- Receipt cleanup values: `{', '.join(sorted(set(cleanup_values))) if cleanup_values else 'no profile cleanup receipt'}`.")
    lines.append("- This job summary is diagnostic visibility; only the strict aggregate establishes qualification.")
    return "\n".join(lines) + "\n"


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
        if args.shard_group not in gate.ROUTINE_SHARD_GROUPS:
            raise gate.GateError('unit worker requires a supported --shard-group')
        shard_path = root / 'shard.json'
        before_tmp = {p.name for p in Path('/tmp').glob(f"fwrouter-gate-{plan['source_commit'][:8]}-*")}
        commands = [('unit', [sys.executable, 'tests/gates/gate.py', 'run', '--plan', str(args.plan),
                              '--include-native', '--defer-hosted', '--shard-group', args.shard_group,
                              '--output', str(shard_path)])]
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
            selected_profiles = native_profiles(plan)
            if args.native_profile not in selected_profiles:
                raise gate.GateError('native worker requires a profile selected by the immutable plan')
            if args.native_profile == 'qcp':
                if not any(r.get('execution_profile') == 'qualified-child-process'
                           and r['path'] != 'backend/tests/test_xray_native_readback.py' for r in rows):
                    raise gate.GateError('QCP profile is not selected by the immutable plan')
                commands = [('qcp', [sys.executable, 'tests/acceptance/qualified_child.py', '--run'])]
            else:
                if 'backend/tests/test_xray_native_readback.py' not in plan['selected_files']:
                    raise gate.GateError('Docker-Xray profile is not selected by the immutable plan')
                commands = [('xray', [sys.executable, 'tests/acceptance/qualified_xray_docker.py', '--run'])]
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
    if args.role == 'unit' and shard_path.is_file():
        report = strict.read_json(shard_path)
        remaining = {p.name for p in Path('/tmp').glob(f"fwrouter-gate-{plan['source_commit'][:8]}-*")}
        report['run_id'] = os.environ['GITHUB_RUN_ID']
        report['run_attempt'] = os.environ['GITHUB_RUN_ATTEMPT']
        report['resources_before'] = strict.read_json(root / 'resources-before.json')
        report['resources_after'] = strict.read_json(root / 'resources-after.json')
        report['process_peak'] = process_peak(root / 'unit.log')
        report['cleanup'] = ('verified_owned_gate_temp_removed' if not (remaining - before_tmp)
                             else 'failed_owned_gate_temp_remains')
        if report['cleanup'] != 'verified_owned_gate_temp_removed':
            report['status'] = 'failed'
            report.setdefault('blocked_reasons', []).append('owned gate temporary directory remains after worker exit')
        report.pop('shard_digest', None)
        report['shard_digest'] = gate.canonical_digest(report)
        gate.atomic_json(shard_path, report)
        summary = routine_shards.write_summary(report)
        summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary_path:
            with Path(summary_path).open('a', encoding='utf-8') as stream:
                stream.write(summary)
        print(summary, end='', flush=True)
        if report['status'] != 'passed':
            status = status or 1
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
        selected_native_profiles = native_profiles(plan)
        expected_dirs = {prefix.format(role=k) for k, needed in required.items()
                         if needed and k not in {'unit', 'native'}}
        if required['unit']:
            expected_dirs.update(prefix.format(role=f"unit-{group}") for group in gate.ROUTINE_SHARD_GROUPS)
        if required['native']:
            expected_dirs.update(prefix.format(role=f"native-{profile}")
                                 for profile in selected_native_profiles)
        if {p.name for p in artifact_root.iterdir()} != expected_dirs:
            raise gate.GateError('missing or unexpected producer artifact directories')
        for role in ROLES:
            if role in {'unit', 'native'}:
                continue
            source = artifact_root / prefix.format(role=role)
            if source.exists():
                source.rename(artifact_root / role)
        args.plan = artifact_root / 'plan' / Path(args.plan).name
        for role, needed in required.items():
            if role == 'unit':
                continue
            if needed:
                directory = Path(args.plan).parent if role == 'plan' else artifact_root / role
                verify_producer(directory, role, plan)
            elif (artifact_root / role).exists():
                raise gate.GateError(f'unexpected artifact for unselected profile {role}')
        if required['unit']:
            unit_root = artifact_root / 'unit'
            unit_root.mkdir(mode=0o700)
            shard_root = unit_root / 'shards'
            shard_root.mkdir(mode=0o700)
            shard_reports = {}
            for group in gate.ROUTINE_SHARD_GROUPS:
                source = artifact_root / prefix.format(role=f'unit-{group}')
                verify_producer(source, 'unit', plan)
                report = strict.read_json(source / 'shard.json')
                routine_shards.verify_shard(
                    report, group, plan, routine_shards.expected_by_group(plan, gate.load_manifest())[group],
                    run_id=os.environ['GITHUB_RUN_ID'], attempt=os.environ['GITHUB_RUN_ATTEMPT'])
                shard_reports[group] = report
                source.rename(shard_root / group)
            merged_unit = routine_shards.merge_reports(
                plan, gate.load_manifest(), shard_reports,
                run_id=os.environ['GITHUB_RUN_ID'], attempt=os.environ['GITHUB_RUN_ATTEMPT'])
            gate.atomic_json(unit_root / 'fwrouter-affected-unit.json', merged_unit)
            seal(unit_root, 'unit', plan, 'success')
        if required['native']:
            native_root = artifact_root / 'native'
            native_root.mkdir(mode=0o700)
            native_manifests = []
            for profile in selected_native_profiles:
                source = artifact_root / prefix.format(role=f"native-{profile}")
                verify_producer(source, 'native', plan)
                profile_manifest = strict.read_json(source / 'runtime-inputs.json')
                strict.validate_runtime_inputs(profile_manifest)
                native_manifests.append(profile_manifest)
                relative = ("qualified-child-receipt.json" if profile == 'qcp'
                            else "qualified-xray-docker-receipt.json")
                receipt = one(source, relative)
                sibling = receipt.with_name('profile.json')
                if sibling.is_symlink() or not sibling.is_file():
                    raise gate.GateError(f'{profile}: native receipt is missing its profile.json closure')
                target = native_root / profile
                target.mkdir(mode=0o700)
                shutil.copyfile(receipt, target / receipt.name, follow_symlinks=False)
                shutil.copyfile(sibling, target / 'profile.json', follow_symlinks=False)
                if profile == selected_native_profiles[0]:
                    shutil.copyfile(source / 'runtime-inputs.json', native_root / 'runtime-inputs.json',
                                    follow_symlinks=False)
                shutil.rmtree(source)
            if any(value != native_manifests[0] for value in native_manifests[1:]):
                raise gate.GateError('native profile runners disagree on pinned runtime input identity')
            seal(native_root, 'native', plan, 'success')
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
    parser.add_argument('command', choices=('configure', 'verify-plan', 'seal', 'worker', 'aggregate', 'summary'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--role', choices=ROLES)
    parser.add_argument('--shard-group', choices=gate.ROUTINE_SHARD_GROUPS)
    parser.add_argument('--native-profile', choices=('qcp', 'xray'))
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
        elif args.command == 'summary':
            print(worker_summary(args.root, args.plan, args.role, args.outcome, args.native_profile), end='')
        else:
            return aggregate(args)
        return 0
    except (OSError, ValueError, gate.GateError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
