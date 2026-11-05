#!/usr/bin/env python3
"""Bounded visibility projection of execution receipts; never grants eligibility."""
from __future__ import annotations

from collections import Counter
import html
import hashlib
import json
import os
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


def collect_partial_routine_shards(evidence: Path, plan: dict[str, Any], manifest: dict[str, Any],
                                  executions: list[dict[str, Any]], groups: list[dict[str, Any]],
                                  problems: list[str]) -> bool:
    """Project only digest- and attempt-bound shard outcomes for failed aggregation."""
    import aggregate_hosted as strict

    expected = set(gate.ROUTINE_SHARD_GROUPS)
    seen: set[str] = set()
    run_id, attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
    candidates = set(evidence.glob('fwrouter-v2-unit-*/shard.json'))
    candidates.update(evidence.glob('fwrouter-l6-unit-*/shard.json'))
    candidates.update(evidence.glob('unit-*/shard.json'))
    if not run_id.isdigit() or not attempt.isdigit():
        problems.append('current run/attempt identity is unavailable for partial routine shards')
        return False
    for path in sorted(candidates):
        root = path.parent
        try:
            producer = strict.read_json(root / 'producer.json')
            strict.verify_digest(producer, 'producer_digest', 'partial unit producer')
            report = read(path)
            strict.verify_digest(report, 'shard_digest', 'partial routine shard')
            group = report.get('shard_group')
            expected_roots = ({f'fwrouter-v2-unit-{group}-{run_id}-{attempt}',
                               f'fwrouter-l6-unit-{group}-{run_id}-{attempt}', f'unit-{group}'})
            if (group not in expected or root.name not in expected_roots or group in seen or producer.get('role') != 'unit'
                    or producer.get('outcome') not in {'success', 'failure', 'cancelled', 'skipped'}
                    or report.get('source_commit') != plan.get('source_commit')
                    or report.get('plan_digest') != plan.get('plan_digest')
                    or producer.get('source_commit') != plan.get('source_commit')
                    or producer.get('plan_digest') != plan.get('plan_digest')
                    or producer.get('manifest_digest') != plan.get('manifest_digest')
                    or producer.get('manifest_version') != plan.get('manifest_version')
                    or report.get('run_id') != producer.get('run_id')
                    or report.get('run_attempt') != producer.get('run_attempt')
                    or (run_id and producer.get('run_id') != run_id)
                    or (attempt and producer.get('run_attempt') != attempt)):
                problems.append('partial routine shard identity or ownership is invalid')
                continue
            manifest_files = producer.get('files')
            shard_file = manifest_files.get('shard.json') if isinstance(manifest_files, dict) else None
            if (set(manifest_files or {}) != {'shard.json'} or not isinstance(shard_file, dict)
                    or shard_file.get('bytes') != path.stat().st_size
                    or shard_file.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest()):
                problems.append('partial routine shard producer inventory is invalid')
                continue
            node_status = report.get('node_status')
            ids = report.get('executed_node_ids')
            rows = report.get('executions')
            skip_reasons = report.get('authorized_skip_reasons')
            if (not isinstance(node_status, dict) or not isinstance(ids, list) or len(ids) != len(set(ids))
                    or not set(ids) <= set(node_status) or not isinstance(rows, list)):
                problems.append('partial routine shard node inventory is invalid')
                continue
            paths = {item.get('path') for item in rows if isinstance(item, dict)
                     and isinstance(item.get('path'), str)
                     and item.get('execution_group') == group}
            observed = {node: node_status[node] for node in ids}
            observed_paths = {node.split('::', 1)[0] for node in ids if isinstance(node, str)}
            try:
                observed_skip_reasons = {node: reason for node, reason in (skip_reasons or {}).items()
                                         if node in observed and observed[node] == 'skipped'}
                gate.validate_authorized_node_skips(manifest, observed, observed_skip_reasons,
                                                    required_paths=observed_paths)
            except gate.GateError:
                problems.append('partial routine shard SKIP policy is invalid')
                continue
            for node in ids:
                status = observed.get(node)
                if (not isinstance(node, str) or status not in STATUSES
                        or node.split('::', 1)[0] not in paths):
                    problems.append('partial routine shard contains an invalid observed case')
                    break
            else:
                catalog = {row['path']: row for row in manifest['test_files']}
                for node in ids:
                    source = node.split('::', 1)[0]
                    owner = catalog.get(source, {}).get('execution_group', 'routine')
                    executions.append({'node_id': node, 'status': observed[node],
                                       **({'skip_reason': observed_skip_reasons[node]} if observed[node] == 'skipped' else {}),
                                       'job': owner, 'profile': 'routine',
                                       'primary_level': catalog.get(source, {}).get('primary_level', 'unknown'),
                                       'domain': catalog.get(source, {}).get('domain', 'unknown'),
                                       'evidence': f'{root.name}/shard.json'})
                groups.append({'shard_group': group, 'executed_node_count': len(ids),
                               'duration_seconds': report.get('duration_seconds'),
                               'cleanup': report.get('cleanup', 'unavailable'),
                               'status': report.get('status', 'incomplete')})
                seen.add(group)
        except (OSError, ValueError, gate.GateError):
            problems.append('partial routine shard is missing, oversized or corrupt')
    if not seen:
        return False
    missing = sorted(expected - seen)
    if missing:
        problems.append(f'partial routine shard results missing for {len(missing)} execution groups')
    return True


def collect(evidence: Path, required: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Summarize both eligible and incomplete evidence without upgrading status."""
    manifest = gate.load_manifest()
    catalog = {row['path']: row for row in manifest['test_files']}
    executions: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    problems: list[str] = []
    host_qualification_status = 'NOT_VERIFIED'
    unit_policy_valid = True
    l5_coverage: Any = 'unavailable'
    unit = evidence / 'unit/fwrouter-affected-unit.json'
    if unit.is_file():
        value = read(unit)
        l5_coverage = value.get('l5_coverage', 'unavailable')
        if value.get('host_qualification_status') != 'NOT_VERIFIED':
            host_qualification_status = 'INVALID_OR_UNAVAILABLE'
            unit_policy_valid = False
            problems.append('routine unit report does not explicitly separate host qualification')
        try:
            gate.validate_authorized_node_skips(
                manifest, value.get('node_status', {}), value.get('authorized_skip_reasons'),
                required_paths=set(plan.get('selected_files', [])),
            )
        except gate.GateError as exc:
            host_qualification_status = 'INVALID_OR_UNAVAILABLE'
            unit_policy_valid = False
            problems.append('unit skip-policy validation failed: ' + str(exc))
        skip_reasons = value.get('authorized_skip_reasons', {})
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
                                       **({'skip_reason': skip_reasons[node]} if status == 'skipped'
                                          and isinstance(skip_reasons, dict) and node in skip_reasons else {}),
                                       'job': row.get('execution_group', 'routine'), 'profile': 'routine',
                                       'primary_level': row.get('primary_level', 'unknown'),
                                       'domain': row.get('domain', 'unknown'),
                                       'evidence': 'unit/fwrouter-affected-unit.json'})
        for item in value.get('routine_shards', []):
            groups.append(dict(item))
    else:
        if not collect_partial_routine_shards(evidence, plan, manifest, executions, groups, problems):
            host_qualification_status = 'INVALID_OR_UNAVAILABLE'
            unit_policy_valid = False
            problems.append('routine unit report is missing; skip policy cannot be verified')
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
    required_result_eligible = result.get('eligible') is True
    strict_aggregate_eligible = strict.get('eligible') is True
    eligible = required_result_eligible and strict_aggregate_eligible
    selected = set(plan.get('selected_files', []))
    observed_files = {item['node_id'].split('::', 1)[0] for item in executions}
    selected_observed_files = selected & observed_files
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
              'run_id': result.get('run_id') or os.environ.get('GITHUB_RUN_ID'),
              'run_attempt': result.get('run_attempt') or os.environ.get('GITHUB_RUN_ATTEMPT'),
              'qualification_status': ('passed' if eligible and unit_policy_valid
                                        else 'incomplete_or_failed'),
              'host_qualification_status': host_qualification_status,
              'strict_aggregate_status': 'passed' if strict_aggregate_eligible else 'incomplete_or_failed',
              'required_result_eligible': required_result_eligible,
              'strict_aggregate_eligible': strict_aggregate_eligible,
              'eligibility_authority': 'strict aggregate; visibility also requires valid unit skip-policy projection',
              'unique_test_coverage': len(unique), 'total_test_executions': len(executions),
              'discovered_suite_file_count': len(catalog),
              'selected_suite_file_count': len(selected),
              'observed_selected_suite_file_count': len(selected_observed_files),
              'statuses': {status: counts[status] for status in STATUSES},
              'levels': by_level, 'groups': groups, 'executions': executions,
              'not_run_selected_files': not_run, 'omitted_files': omitted,
              'diagnostic_limitations': problems,
              'baseline_classification': strict.get('baseline_classification', 'unavailable'),
              'l5_coverage': l5_coverage}
    report['visibility_digest'] = gate.canonical_digest(report)
    return report


def summary(report: dict[str, Any], *, profile: str = 'profile unavailable', run_url: str = '') -> str:
    """Render a compact human projection; detailed IDs remain in machine evidence."""
    escape = lambda value: html.escape(str(value).replace('\n', ' ').replace('\r', ' '), quote=True).replace('|', '&#124;').replace('`', '&#96;')
    executions = report.get('executions') if isinstance(report.get('executions'), list) else []
    known_statuses = ('passed', 'failed', 'skipped', 'blocked', 'not_run')
    status_counts = {key: 0 for key in known_statuses}
    unique_ids: set[str] = set()
    failed_nodes: list[tuple[str, str]] = []
    authorized_skips: Counter[str] = Counter()
    for item in executions:
        if not isinstance(item, dict):
            continue
        node = item.get('node_id')
        status = item.get('status')
        if isinstance(node, str):
            unique_ids.add(node)
            if status in {'failed', 'blocked', 'not_run'}:
                failed_nodes.append((node, status))
        if status in status_counts:
            status_counts[status] += 1
        if status == 'skipped':
            reason = item.get('skip_reason')
            code = reason.get('reason_code') if isinstance(reason, dict) else None
            authorized_skips[code if isinstance(code, str) and code else 'reason unavailable'] += 1
    total = report.get('total_test_executions')
    unique = report.get('unique_test_coverage')
    repeated = total - unique if type(total) is int and type(unique) is int and total >= unique >= 0 else None
    selected_suites = report.get('selected_suite_file_count')
    observed_suites = report.get('observed_selected_suite_file_count')
    not_run_suites = report.get('not_run_selected_files')
    if not isinstance(not_run_suites, list):
        not_run_suites = []
    lines = ['## CI test execution', '', f"- Profile: **{escape(profile)}**",
             f"- Qualification: **{escape(report.get('qualification_status', 'INCOMPLETE'))}** (strict aggregate authority)",
             f"- Host qualification: **{escape(report.get('host_qualification_status', 'NOT_VERIFIED'))}**",
             f"- Source SHA: `{escape(report.get('source_commit', 'unavailable'))}`; run `{escape(report.get('run_id', 'unavailable'))}` / attempt `{escape(report.get('run_attempt', 'unavailable'))}`"]
    if run_url:
        lines.append(f"- [Open workflow run and job logs]({escape(run_url)})")
    discovered_suites = report.get('discovered_suite_file_count')
    lines += [f"- Discovered suite files (manifest): **{discovered_suites if type(discovered_suites) is int else 'NOT AVAILABLE'}**; selected suite files: **{selected_suites if type(selected_suites) is int else 'NOT AVAILABLE'}**; with observed case IDs: **{observed_suites if type(observed_suites) is int else 'NOT AVAILABLE'}**; selected suite files without observed case IDs: **{len(not_run_suites)}**",
              f"- Observed case IDs: **{unique if type(unique) is int else 'NOT AVAILABLE'}** unique; **{total if type(total) is int else 'NOT AVAILABLE'}** terminal outcomes; **{repeated if repeated is not None else 'NOT AVAILABLE'}** repeated executions",
              f"- Actual executed PASS+FAIL case executions (SKIP excluded): **{status_counts['passed'] + status_counts['failed']}**",
              f"- Outcomes: PASS {status_counts['passed']}; FAIL {status_counts['failed']}; SKIP {status_counts['skipped']}; BLOCKED {status_counts['blocked']}; NOT RUN {status_counts['not_run']}",
              f"- Active host SKIPs (host qualification remains NOT_VERIFIED): {', '.join(f'{escape(key)} ({value})' for key, value in sorted(authorized_skips.items()) if key == 'host_environment_unavailable') or 'none observed'}",
              f"- Retired QEMU SKIPs: {', '.join(f'{escape(key)} ({value})' for key, value in sorted(authorized_skips.items()) if key == 'retired_qemu') or 'none observed'}",
              f"- Other authorized SKIP groups: {', '.join(f'{escape(key)} ({value})' for key, value in sorted(authorized_skips.items()) if key not in {'host_environment_unavailable', 'retired_qemu'}) or 'none observed'}",
              '- Line/branch coverage: **NOT COLLECTED**; ' + _summary_l5_coverage(report.get('l5_coverage')), '',
              '| Primary level | Unique case IDs | Observed outcomes |', '|---|---:|---:|']
    levels = report.get('levels') if isinstance(report.get('levels'), dict) else {}
    for level in ('L1', 'L2', 'L3', 'L4'):
        row = levels.get(level, {})
        if isinstance(row, dict):
            lines.append(f"| {level} | {row.get('unique', 'NOT AVAILABLE')} | {escape(row.get('statuses', 'NOT AVAILABLE'))} |")
    lines += ['', '| Execution group/profile | Duration (seconds) | Status |', '|---|---:|---|']
    groups = report.get('groups') if isinstance(report.get('groups'), list) else []
    for row in groups[:24]:
        if isinstance(row, dict):
            duration = row.get('duration_seconds')
            duration = f"{duration:.3f}" if type(duration) in (int, float) else 'NOT AVAILABLE'
            lines.append(f"| {escape(row.get('shard_group', 'unknown'))} | {duration} | {escape(row.get('status', 'unknown'))} |")
    if len(groups) > 24:
        lines.append(f"| Additional groups omitted | {len(groups) - 24} | see machine evidence |")
    lines += ['', '### Observed failures and incomplete cases']
    for node, status in failed_nodes[:12]:
        node_label = gate.redact_failure_output(node).replace('\n', ' ').replace('\r', ' ')
        lines.append(f"- `{escape(node_label[:240])}` — {escape(status)}")
    if len(failed_nodes) > 12:
        lines.append(f"- {len(failed_nodes) - 12} additional non-passing case IDs are in machine evidence.")
    if not failed_nodes:
        lines.append('- No non-passing observed case IDs recorded.')
    problems = report.get('diagnostic_limitations') if isinstance(report.get('diagnostic_limitations'), list) else []
    for reason in problems[:5]:
        if isinstance(reason, str):
            redacted = gate.redact_failure_output(reason).replace('\n', ' ').replace('\r', ' ')
            lines.append(f"- Evidence limitation: {escape(redacted[:240])}")
    lines += ['', f"- Visibility digest: `{escape(report.get('visibility_digest', 'unavailable'))}`",
              '', 'Visibility is a projection of observed receipts, not an independent PASS or deployment authorization.', '']
    return '\n'.join(lines)


def _summary_l5_coverage(value: Any) -> str:
    """Project L5 coverage counts only; anchor node IDs stay in machine evidence."""
    if isinstance(value, dict):
        anchors = value.get('anchors')
        required = value.get('required_count')
        if not isinstance(anchors, list) or type(required) is not int or required < 0:
            return 'L5 regression coverage: NOT AVAILABLE'
        passed = sum(isinstance(row, dict) and row.get('status') == 'passed' for row in anchors)
        status = value.get('status')
        if status not in {'passed', 'incomplete_or_failed'}:
            status = 'unavailable'
        return f"L5 regression anchors: {passed}/{required} passed ({status})"
    if isinstance(value, str):
        return 'L5 regression coverage: ' + html.escape(
            gate.redact_failure_output(value).replace('\n', ' ').replace('\r', ' ')[:160], quote=True)
    return 'L5 regression coverage: NOT AVAILABLE'
