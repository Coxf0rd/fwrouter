#!/usr/bin/env python3
"""Bounded visibility projection of execution receipts; never grants eligibility."""
from __future__ import annotations

from collections import Counter
import html
import json
from pathlib import Path
from typing import Any

import gate

MAX_JSON = 8 * 1024 * 1024
STATUSES = ('passed', 'failed', 'skipped', 'blocked', 'not_run')


def read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise gate.GateError('visibility input missing, unsafe or oversized')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise gate.GateError('visibility input must be an object')
    return value


def receipt_nodes(value: dict[str, Any]) -> dict[str, str]:
    """Use observed results only; expected IDs alone never prove execution."""
    tests = value.get('tests', {})
    if not isinstance(tests, dict):
        return {}
    statuses = tests.get('node_status')
    if isinstance(statuses, dict):
        return {str(node): status if status in STATUSES else 'blocked'
                for node, status in statuses.items()}
    ids = tests.get('nodeids')
    # Hosted JUnit receipts expose IDs plus aggregate counters. Attribute PASS
    # only when every observed case passed and the strict receipt is passed.
    if (isinstance(ids, list) and len(ids) == len(set(ids))
            and all(isinstance(node, str) for node in ids)
            and value.get('status') == 'passed'
            and tests.get('tests') == len(ids)
            and all(tests.get(key) == 0 for key in ('errors', 'failures', 'skipped'))):
        return {node: 'passed' for node in ids}
    return {}


def collect(evidence: Path, required: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Summarize both eligible and incomplete evidence without upgrading status."""
    manifest = gate.load_manifest()
    catalog = {row['path']: row for row in manifest['test_files']}
    executions: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    problems: list[str] = []
    l5_coverage: Any = 'unavailable'
    unit = evidence / 'unit/fwrouter-affected-unit.json'
    if unit.is_file():
        value = read(unit)
        l5_coverage = value.get('l5_coverage', 'unavailable')
        markers = set(value.get('file_marker_paths', []))
        for item in value.get('executions', []):
            if not isinstance(item, dict) or item.get('path') == '@L0':
                continue
            path = item.get('path')
            row = catalog.get(path, {})
            statuses = item.get('node_status', {})
            if not isinstance(statuses, dict):
                statuses = {}
            ids = set(item.get('executed_node_ids', []))
            for node, status in statuses.items():
                if node in ids and node not in markers:
                    executions.append({'node_id': node, 'status': status if status in STATUSES else 'blocked',
                                       'job': row.get('execution_group', 'routine'), 'profile': 'routine',
                                       'primary_level': row.get('primary_level', 'unknown'),
                                       'domain': row.get('domain', 'unknown'),
                                       'evidence': 'unit/fwrouter-affected-unit.json'})
        for item in value.get('routine_shards', []):
            groups.append(dict(item))
    seen_receipts: set[str] = set()
    for name in ('hosted-acceptance-report.json', 'qualified-child-receipt.json',
                 'qualified-xray-docker-receipt.json'):
        for path in sorted(evidence.rglob(name)):
            # Retained native child copies are the original evidence; combined
            # native roots can also retain the same receipt for strict replay.
            value = read(path)
            receipt_digest = gate.canonical_digest(value)
            if receipt_digest in seen_receipts:
                continue
            seen_receipts.add(receipt_digest)
            nodes = receipt_nodes(value)
            identity = value.get('suite_nonce')
            profile = value.get('suite', value.get('scope', name))
            for node, status in nodes.items():
                row = catalog.get(node.split('::', 1)[0], {})
                executions.append({'node_id': node, 'status': status, 'job': str(profile),
                                   'profile': row.get('execution_profile', str(profile)),
                                   'primary_level': row.get('primary_level', 'unknown'),
                                   'domain': row.get('domain', 'unknown'),
                                   'invocation': identity, 'evidence': path.relative_to(evidence).as_posix()})
            groups.append({'shard_group': str(profile), 'executed_node_count': len(nodes),
                           'duration_seconds': value.get('elapsed_seconds', value.get('pytest_elapsed_seconds')),
                           'cleanup': value.get('cleanup', 'unavailable'),
                           'status': value.get('status', 'incomplete')})
            if not nodes:
                problems.append(f'{profile}: no attributable observed node results')
    result_path = required / 'required-result.json'
    strict_path = required / 'fwrouter-aggregate.json'
    result = read(result_path) if result_path.is_file() else {}
    strict = read(strict_path) if strict_path.is_file() else {}
    eligible = result.get('eligible') is True and strict.get('eligible') is True
    selected = set(plan.get('selected_files', []))
    observed_files = {item['node_id'].split('::', 1)[0] for item in executions}
    omitted = [{'path': path, 'reason': 'not selected by immutable plan'}
               for path in sorted(set(catalog) - selected)]
    not_run = sorted(selected - observed_files)
    counts = Counter(item['status'] for item in executions)
    unique: dict[str, list[str]] = {}
    for item in executions:
        unique.setdefault(item['node_id'], []).append(item['status'])
    by_level = {}
    for level in ('L1', 'L2', 'L3', 'L4'):
        items = [item for item in executions if item['primary_level'] == level]
        by_level[level] = {'unique': len({item['node_id'] for item in items}),
                           'executions': len(items), 'statuses': dict(Counter(item['status'] for item in items))}
    report = {'schema': 'fwrouter-ci-visibility/v1', 'source_commit': plan.get('source_commit'),
              'plan_digest': plan.get('plan_digest'), 'manifest_digest': plan.get('manifest_digest'),
              'run_id': result.get('run_id'), 'run_attempt': result.get('run_attempt'),
              'qualification_status': 'passed' if eligible else 'incomplete_or_failed',
              'eligibility_authority': 'strict aggregate; visibility cannot establish eligibility',
              'unique_test_coverage': len(unique), 'total_test_executions': len(executions),
              'statuses': {status: counts[status] for status in STATUSES},
              'levels': by_level, 'groups': groups, 'executions': executions,
              'not_run_selected_files': not_run, 'omitted_files': omitted,
              'diagnostic_limitations': problems,
              'baseline_classification': strict.get('baseline_classification', 'unavailable'),
              'l5_coverage': l5_coverage}
    report['visibility_digest'] = gate.canonical_digest(report)
    return report


def summary(report: dict[str, Any]) -> str:
    escape = lambda value: html.escape(str(value)).replace('|', '&#124;')
    lines = ['## CI V3 test execution', '',
             f"- Qualification: **{escape(report['qualification_status'])}** (strict aggregate authority)",
             f"- SHA `{report.get('source_commit')}`; run `{report.get('run_id')}` / attempt `{report.get('run_attempt')}`",
             f"- Unique observed test IDs: **{report['unique_test_coverage']}**; total executions: **{report['total_test_executions']}**",
             '- ' + '; '.join(f'{key.upper()}: {value}' for key, value in report['statuses'].items()),
             f"- Selected files without observed case results: {len(report['not_run_selected_files'])}; unselected files: {len(report['omitted_files'])}",
             f"- Visibility digest: `{report['visibility_digest']}`", '',
             '| Primary level | Unique IDs | Executions | Statuses |', '|---|---:|---:|---|']
    for level, row in report['levels'].items():
        lines.append(f"| {level} | {row['unique']} | {row['executions']} | {escape(row['statuses'])} |")
    lines += ['', '| Execution group/profile | Seconds | Cleanup |', '|---|---:|---|']
    for row in report['groups']:
        lines.append(f"| {escape(row.get('shard_group'))} | {escape(row.get('duration_seconds', 'unavailable'))} | {escape(row.get('cleanup', 'unavailable'))} |")
    lines += ['', '### L5 regression coverage', '', escape(report['l5_coverage']), '',
              '### Missing/omitted coverage', '',
              '<details><summary>Selected files without observed test cases</summary><pre>' +
              escape(json.dumps(report['not_run_selected_files'])) + '</pre></details>',
              '<details><summary>Unselected files and immutable-plan reasons</summary><pre>' +
              escape(json.dumps(report['omitted_files'])) + '</pre></details>',
              '', 'Visibility is a projection of observed receipts, not an independent PASS or deployment authorization.', '']
    return '\n'.join(lines)
